import copy
import logging
import os
from typing import Dict, Tuple, Union

import numpy as np
import torch
import torchmetrics
from peft import get_peft_model, PeftConfig, PeftModel, PromptTuningConfig  # get_peft_config,
from transformers import AutoModelForCausalLM, GenerationConfig

from ludwig.constants import LOGITS, MODEL_LLM
from ludwig.features.base_feature import OutputFeature
from ludwig.features.text_feature import TextOutputFeature
from ludwig.globals import MODEL_WEIGHTS_FILE_NAME
from ludwig.models.base import BaseModel
from ludwig.schema.features.base import BaseOutputFeatureConfig, FeatureCollection
from ludwig.schema.model_types.llm import LLMModelConfig
from ludwig.utils.augmentation_utils import AugmentationPipelines
from ludwig.utils.data_utils import clear_data_cache
from ludwig.utils.output_feature_utils import set_output_feature_tensor

logger = logging.getLogger(__name__)


class LLM(BaseModel):
    @staticmethod
    def type() -> str:
        return MODEL_LLM

    def __init__(
        self,
        config_obj: LLMModelConfig,
        random_seed=None,
        **_kwargs,
    ):
        super().__init__(random_seed=random_seed)

        self.config_obj = config_obj
        self._random_seed = random_seed

        self.adapter = copy.deepcopy(self.config_obj.adapter)

        self.model_name = self.config_obj.model_name

        print("Loading large language model...")
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_name,
            low_cpu_mem_usage=True,
            torch_dtype=torch.float16,
            device_map="auto",
            max_memory={i: "13GiB" for i in range(4)},
        )
        print("Done loading large language model...")

        # Determines the maximum length of the context (input + output tokens)
        if hasattr(self.model.config, "max_sequence_length"):
            self.context_len = self.model.config.max_sequence_length
        elif hasattr(self.model.config, "max_position_embeddings"):
            self.context_len = self.model.config.max_position_embeddings
        else:
            self.context_len = 2048
        print("Context length:", self.context_len)

        # If an adapter config is provided, we want to wrap the model with a PEFT model
        # for fine-tuning.
        if self.config_obj.adapter:
            # TODO: Refactor once adapter is a config object instead of a dict
            self.adapter["peft_type"] = self.adapter.pop("type").upper()
            self.adapter["tokenizer_name_or_path"] = self.model_name
            # TODO: Figure out how to use peft_model_config properly
            self.model = get_peft_model(self.model, PromptTuningConfig(**self.adapter))
            logger.info("Trainable Parameters For Fine-Tuning:")
            self.model.print_trainable_parameters()

        # Initialize the generation config to use for generation calls.
        self.generation_config = GenerationConfig(**self.config_obj.generation_config.to_dict())
        self.max_new_tokens = self.config_obj.generation_config.max_new_tokens
        self.max_input_length = self.context_len - self.max_new_tokens - 8

        # ================ Inputs ================
        try:
            self.input_features.update(self.build_inputs(input_feature_configs=self.config_obj.input_features))
        except KeyError as e:
            raise KeyError(
                f"An input feature has a name that conflicts with a class attribute of torch's ModuleDict: {e}"
            )

        # ================ Outputs ================
        self.output_features.update(
            self.build_outputs(
                output_feature_configs=self.config_obj.output_features,
                # Set the input size to the model vocab size instead of the tokenizer vocab size
                # because the model has additional "head" layers that are used to predict the next
                # token in the sequence. These head layers can add additional dimensions to the
                # logits tensor, beyond the vocab_size dimension.
                input_size=self.model.config.vocab_size,
            )
        )

        # Extract the decoder object for the forward pass
        _, self.output_feature_decoder = self.output_features.items()[0]

        # ================ Combined loss metric ================
        self.eval_loss_metric = torchmetrics.MeanMetric()
        self.eval_additional_losses_metrics = torchmetrics.MeanMetric()

        clear_data_cache()

    @classmethod
    def build_outputs(
        cls, output_feature_configs: FeatureCollection[BaseOutputFeatureConfig], input_size: int
    ) -> Dict[str, OutputFeature]:
        """Builds and returns output feature."""
        # TODO: only single task currently
        if len(output_feature_configs) > 1:
            raise ValueError("Only single task currently supported")

        output_feature_config = output_feature_configs[0]
        output_feature_config.input_size = input_size

        output_features = {}
        output_feature = cls.build_single_output(output_feature_config, output_features)
        output_features[output_feature_config.name] = output_feature

        return output_features

    def get_input_ids(
        self,
        inputs: Union[
            Dict[str, torch.Tensor], Dict[str, np.ndarray], Tuple[Dict[str, torch.Tensor], Dict[str, torch.Tensor]]
        ],
    ):
        """Returns the input ids for the text feature input."""
        return inputs[self.config_obj.input_features[0].name].type(torch.int32)

    def forward(
        self,
        inputs: Union[
            Dict[str, torch.Tensor], Dict[str, np.ndarray], Tuple[Dict[str, torch.Tensor], Dict[str, torch.Tensor]]
        ],
        mask=None,
    ) -> Dict[str, torch.Tensor]:
        """Forward pass of the model.

        Args:
            inputs: Inputs to the model. Can be a dictionary of input names to
                input tensors or a tuple of (inputs, targets) where inputs is
                a dictionary of input names to input tensors and targets is a
                dictionary of target names to target tensors.
            mask: A mask for the inputs.

        Returns:
            A dictionary of output {feature name}::{tensor_name} -> output tensor.
        """

        if isinstance(inputs, tuple):
            inputs, targets = inputs
            # Convert targets to tensors.
            for target_feature_name, target_value in targets.items():
                if not isinstance(target_value, torch.Tensor):
                    targets[target_feature_name] = torch.from_numpy(target_value)
                else:
                    targets[target_feature_name] = target_value
        else:
            targets = None

        assert list(inputs.keys()) == self.input_features.keys()

        input_ids = self.get_input_ids(inputs)
        input_ids = input_ids[:, -self.max_input_length :]
        input_ids = input_ids.to(self.model.device)

        print(">>>>> Model device:", self.model.device)
        print(">>>>> Input IDs device:", input_ids.device)

        if self.adapter:
            # Forward pass using PEFT model for fine-tuning
            model_outputs = self.model(input_ids)
            # Pass generated tokens through decoder after averaging the token probabilities
            logits_with_averaged_token_probabilities = torch.mean(model_outputs[LOGITS], dim=1, dtype=torch.float32).to(
                self.model.device
            )
            decoder_outputs = self.output_feature_decoder.decoder_obj(logits_with_averaged_token_probabilities)
            # Set the output feature tensor to the decoder outputs (logits)
            outputs = {}
            set_output_feature_tensor(outputs, self.config_obj.output_features[0].name, LOGITS, decoder_outputs)
            return outputs
        else:
            with torch.no_grad():
                # Generate text using the model
                outputs = self.model.generate(
                    input_ids=input_ids,
                    attention_mask=mask,
                    generation_config=self.generation_config,
                    return_dict_in_generate=True,
                    output_scores=True,
                )
                # Extract the predictions, probabilities and logits from the model outputs
                # through the forward pass of the output feature
                outputs = self.output_feature_decoder.decoder_obj(
                    outputs,
                    llm_model_inputs=input_ids,
                )
                return self.extract(outputs)

    def extract(
        self,
        outputs,
    ):
        """Extracts predictions and probabilities from the model outputs."""
        return {self.config_obj.output_features[0].name: outputs}

    def update_metrics(self, targets, predictions):
        """Updates the model's metrics given targets and predictions."""
        for of_name, of_obj in self.output_features.items():
            if isinstance(of_obj, TextOutputFeature):
                # Align the target length with the predictions length to enable text metric evaluation.
                _targets = self._realign_target_tensor(targets, predictions, of_name)
                of_obj.update_metrics(_targets[of_name], predictions[of_name])
                continue
            of_obj.update_metrics(targets[of_name], predictions[of_name])

        # Only update loss during fine-tuning since logits are computed during the LLMs forward pass.
        if self.adapter:
            eval_loss, additional_losses = self.eval_loss(targets, predictions)
            self.eval_loss_metric.update(eval_loss)
            self.eval_additional_losses_metrics.update(additional_losses)

    def eval_loss(self, targets, predictions):
        """Computes all evaluation losses for the model given targets and predictions.

        Args:
            targets: A dictionary of target names to target tensors.
            predictions: A dictionary of output names to output tensors.

        Returns:
            A tuple of loss values for eval losses and additional losses.
        """
        eval_loss = 0
        for of_name, of_obj in self.output_features.items():
            if isinstance(of_obj, TextOutputFeature):
                # Align the target length with the predictions length to enable text metric evaluation.
                _targets = self._realign_target_tensor(targets, predictions, of_name)
                of_eval_loss = of_obj.eval_loss(_targets[of_name], predictions[of_name])
            else:
                targets[of_name] = targets[of_name].to("cpu")
                for k, v in predictions[of_name].items():
                    predictions[of_name][k] = v.to("cpu")
                of_eval_loss = of_obj.eval_loss(targets[of_name], predictions[of_name])
            eval_loss += of_obj.loss.weight * of_eval_loss

        additional_loss = 0
        additional_losses = self.losses()
        if additional_losses:
            additional_loss = torch.sum(torch.stack(additional_losses))  # other losses

        return eval_loss, additional_loss

    def outputs_to_predictions(self, outputs: Dict[str, torch.Tensor]) -> Dict[str, Dict[str, torch.Tensor]]:
        """Returns the model's predictions for each output feature."""
        predictions = {}
        for of_name in self.output_features:
            if self.adapter:
                predictions[of_name] = self.output_features.get(of_name).predictions(outputs, of_name)
            else:
                # TODO: Format with the :: notation in the forward pass
                generated_predictions = outputs[of_name]
                predictions[of_name] = generated_predictions
        return predictions

    def save(self, save_path):
        """Saves the model to the given path."""
        if self.adapter:
            weights_save_path = os.path.join(save_path, MODEL_WEIGHTS_FILE_NAME)
            self.model.save_pretrained(weights_save_path)

    def load(self, save_path):
        """Loads the model from the given path."""
        if self.adapter:
            weights_save_path = os.path.join(save_path, MODEL_WEIGHTS_FILE_NAME)
            config = PeftConfig.from_pretrained(weights_save_path)
            config.inference_mode = False
            self.model = AutoModelForCausalLM.from_pretrained(config.base_model_name_or_path)
            self.model = PeftModel.from_pretrained(self.model, weights_save_path)

    def get_args(self):
        """Returns init arguments for constructing this model."""
        return (
            self.config_obj.input_features.to_list(),
            self.config_obj.output_features.to_list(),
            self._random_seed,
        )

    def _realign_target_tensor(self, targets, predictions, of_name: str):
        """Realigns the target tensor with the predictions.

        This is necessary for text metrics that require the target and prediction
        to be of the same length.

        Args:
            targets: The target tensor.
            predictions: The prediction tensor.

        Returns:
            The realigned target tensor.
        """
        _targets = copy.deepcopy(targets)
        _targets[of_name] = torch.nn.functional.pad(
            _targets.get(of_name),
            (0, predictions[of_name].get("predictions").size()[1] - _targets.get(of_name).size()[1]),
            "constant",
            0,
        )
        return _targets

    def get_augmentation_pipelines(self) -> AugmentationPipelines:
        """Returns the augmentation pipeline for this model."""
        return AugmentationPipelines({})
