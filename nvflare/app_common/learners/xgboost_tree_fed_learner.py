# Copyright (c) 2021-2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
from abc import abstractmethod

import xgboost as xgb
from torch.utils.tensorboard import SummaryWriter

from nvflare.apis.dxo import DXO, DataKind, from_shareable
from nvflare.apis.fl_constant import FLContextKey, ReturnCode
from nvflare.apis.fl_context import FLContext
from nvflare.apis.shareable import Shareable, make_reply
from nvflare.apis.signal import Signal
from nvflare.app_common.abstract.learner_spec import Learner
from nvflare.app_common.app_constant import AppConstants
from nvflare.app_common.shareablegenerators.xgb_model_shareable_generator import update_model


class XGBoostTreeFedLearner(Learner):
    def __init__(
        self,
        training_mode,
        num_tree_bagging: int = 1,
        lr_mode: str = "uniform",
        local_model_path: str = "model.json",
        global_model_path: str = "model_global.json",
        learning_rate: float = 0.1,
        objective: str = "binary:logistic",
        max_depth: int = 8,
        eval_metric: str = "auc",
        nthread: int = 16,
        tree_method: str = "hist",
        train_task_name: str = AppConstants.TASK_TRAIN,
    ):
        super().__init__()
        self.training_mode = training_mode
        self.num_tree_bagging = num_tree_bagging
        self.lr_mode = lr_mode
        self.local_model_path = local_model_path
        self.global_model_path = global_model_path
        self.base_lr = learning_rate
        self.objective = objective
        self.max_depth = max_depth
        self.eval_metric = eval_metric
        self.nthread = nthread
        self.tree_method = tree_method
        self.train_task_name = train_task_name
        # Currently we support boosting 1 tree per round
        # could further extend
        self.trees_per_round = 1

    def initialize(self, parts: dict, fl_ctx: FLContext):
        # when a run starts, this is where the actual settings get initialized for learner

        # set the paths according to fl_ctx
        engine = fl_ctx.get_engine()
        ws = engine.get_workspace()
        app_dir = ws.get_app_dir(fl_ctx.get_job_id())
        self.local_model_path = os.path.join(app_dir, self.local_model_path)
        self.global_model_path = os.path.join(app_dir, self.global_model_path)

        # get and print the args
        fl_args = fl_ctx.get_prop(FLContextKey.ARGS)
        self.client_id = fl_ctx.get_identity_name()
        self.log_info(
            fl_ctx,
            f"Client {self.client_id} initialized with args: \n {fl_args}",
        )

        # set local tensorboard writer for local training info of current model
        self.writer = SummaryWriter(app_dir)
        # set the training-related contexts
        # use dynamic shrinkage - adjusted by personalized scaling factor
        if self.lr_mode not in ["uniform", "scaled"]:
            self.log_error(
                fl_ctx,
                f"Only support [uniform] or [scaled] mode, but got {self.lr_mode}",
            )
            return make_reply(ReturnCode.TASK_ABORTED)

        if self.training_mode not in ["cyclic", "bagging"]:
            self.log_error(
                fl_ctx,
                f"Only support [cyclic] or [bagging] mode, but got {self.training_mode}",
            )
            return make_reply(ReturnCode.TASK_ABORTED)

        # load data and lr_scale, this is task/site-specific
        self.load_data(fl_ctx)
        self.set_lr()
        self.bst = None
        self.global_model_as_dict = None

    def set_lr(self):
        if self.training_mode == "bagging":
            # Bagging mode
            if self.lr_mode == "uniform":
                # uniform lr, gloabl learnining_rate scaled by num_tree_bagging for bagging
                self.lr = self.base_lr / self.num_tree_bagging
            elif self.lr_mode == "scaled":
                # scaled lr, global learning_rate scaled by data size percentage
                self.lr = self.base_lr * self.lr_scale
        elif self.training_mode == "cyclic":
            # Cyclic mode, directly use the base learning_rate
            self.lr = self.base_lr

    @abstractmethod
    def load_data(self, fl_ctx: FLContext):
        """Load data customized to individual tasks
        This can be specified / loaded in any ways
        as long as they are made available for training and validation
        Items needed:
        self.dmat_train: xgb.DMatrix
        self.dmat_valid: xgb.DMatrix
        self.valid_y: array for validation metric computation
        self.lr_scale: scale factor needed for site-wise learning rate adjustment
        """
        raise NotImplementedError

    def get_training_parameters_single(self):
        param = {}
        param["objective"] = self.objective
        param["eta"] = self.lr
        param["max_depth"] = self.max_depth
        param["eval_metric"] = self.eval_metric
        param["nthread"] = self.nthread
        param["tree_method"] = self.tree_method
        return param

    def local_boost_bagging(self, param, fl_ctx: FLContext):
        eval_results = self.bst.eval_set(
            evals=[(self.dmat_train, "train"), (self.dmat_valid, "valid")], iteration=self.bst.num_boosted_rounds() - 1
        )
        self.log_info(fl_ctx, eval_results)
        auc = float(eval_results.split("\t")[2].split(":")[1])
        for i in range(self.trees_per_round):
            self.bst.update(self.dmat_train, self.bst.num_boosted_rounds())

        # extract newly added self.trees_per_round using xgboost slicing api
        bst = self.bst[self.bst.num_boosted_rounds() - self.trees_per_round : self.bst.num_boosted_rounds()]

        self.log_info(
            fl_ctx,
            f"Global AUC {auc}",
        )
        # note: writing auc before current training step, for passed in global model
        self.writer.add_scalar(
            "AUC", auc, int((self.bst.num_boosted_rounds() - self.trees_per_round - 1) / self.num_tree_bagging)
        )
        return bst

    def local_boost_cyclic(self, param, fl_ctx: FLContext):
        # Cyclic mode
        # starting from global model
        # return the whole boosting tree series
        self.bst.update(self.dmat_train, self.bst.num_boosted_rounds())
        eval_results = self.bst.eval_set(
            evals=[(self.dmat_train, "train"), (self.dmat_valid, "valid")], iteration=self.bst.num_boosted_rounds() - 1
        )
        self.log_info(fl_ctx, eval_results)
        auc = float(eval_results.split("\t")[2].split(":")[1])
        self.log_info(
            fl_ctx,
            f"Client {self.client_id} AUC after training: {auc}",
        )
        self.writer.add_scalar("AUC", auc, self.bst.num_boosted_rounds() - 1)
        return self.bst

    def train(
        self,
        shareable: Shareable,
        fl_ctx: FLContext,
        abort_signal: Signal,
    ) -> Shareable:

        if abort_signal.triggered:
            self.finalize(fl_ctx)
            return make_reply(ReturnCode.TASK_ABORTED)

        # retrieve current global model download from server's shareable
        dxo = from_shareable(shareable)
        model_update = dxo.data

        # xgboost parameters
        param = self.get_training_parameters_single()

        if not self.bst:
            # First round
            self.log_info(
                fl_ctx,
                f"Client {self.client_id} initial training from scratch",
            )
            if not model_update:
                bst = xgb.train(
                    param,
                    self.dmat_train,
                    num_boost_round=self.trees_per_round,
                    evals=[(self.dmat_valid, "validate"), (self.dmat_train, "train")],
                )
            else:
                loadable_model = bytearray(model_update["model_data"])
                bst = xgb.train(
                    param,
                    self.dmat_train,
                    num_boost_round=self.trees_per_round,
                    xgb_model=loadable_model,
                    evals=[(self.dmat_valid, "validate"), (self.dmat_train, "train")],
                )
            self.config = bst.save_config()
            self.bst = bst
        else:
            self.log_info(
                fl_ctx,
                f"Client {self.client_id} model updates received from server",
            )
            if self.training_mode == "bagging":
                model_updates = model_update["model_data"]
                for update in model_updates:
                    self.global_model_as_dict = update_model(self.global_model_as_dict, json.loads(update))

                loadable_model = bytearray(json.dumps(self.global_model_as_dict), "utf-8")
            else:
                loadable_model = bytearray(model_update["model_data"])

            self.log_info(
                fl_ctx,
                f"Client {self.client_id} converted global model to json ",
            )

            self.bst.load_model(loadable_model)
            self.bst.load_config(self.config)

            self.log_info(
                fl_ctx,
                f"Client {self.client_id} loaded global model into booster ",
            )

            # train local model starting with global model
            if self.training_mode == "bagging":
                bst = self.local_boost_bagging(param, fl_ctx)
            elif self.training_mode == "cyclic":
                bst = self.local_boost_cyclic(param, fl_ctx)

        self.local_model = bst.save_raw("json")

        # report updated model in shareable

        dxo = DXO(data_kind=DataKind.XGB_MODEL, data={"model_data": self.local_model})
        self.log_info(fl_ctx, "Local epochs finished. Returning shareable")
        new_shareable = dxo.to_shareable()

        self.writer.flush()
        return new_shareable

    def finalize(self, fl_ctx: FLContext):
        # freeing resources in finalize avoids seg fault during shutdown of gpu mode
        del self.bst
        del self.dmat_train
        del self.dmat_valid
        self.log_info(fl_ctx, "Freed training resources")
