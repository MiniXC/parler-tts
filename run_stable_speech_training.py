#!/usr/bin/env python
# coding=utf-8
# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
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

""" Train a text-to-speech model using 🤗 Transformers Seq2SeqTrainer"""

import functools
import json
import logging
import os
import re
import sys
import shutil
import warnings
import math
import time
from multiprocess import set_start_method


import evaluate
from tqdm import tqdm
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union, Set

import datasets
import numpy as np
import torch
from torch.utils.data import DataLoader

from datasets import DatasetDict, load_dataset, Dataset, IterableDataset, interleave_datasets, concatenate_datasets

from huggingface_hub import Repository, create_repo
import transformers
from transformers import (
    AutoFeatureExtractor,
    AutoModel,
    AutoModelWithLMHead,
    AutoProcessor,
    AutoTokenizer,
    HfArgumentParser,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
)
from transformers.trainer_utils import is_main_process
from transformers import pipeline
from transformers.optimization import get_scheduler
from transformers.utils import check_min_version, send_example_telemetry
from transformers.utils.versions import require_version
from transformers.integrations import is_wandb_available

from accelerate import Accelerator
from accelerate.utils import set_seed


from stable_speech import StableSpeechForConditionalGeneration, StableSpeechConfig, apply_delay_pattern_mask, build_delay_pattern_mask

if is_wandb_available():
    from wandb import Audio



# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.38.0.dev0")

require_version("datasets>=1.18.0", "To fix: pip install -r examples/pytorch/speech-recognition/requirements.txt")


logger = logging.getLogger(__name__)


def list_field(default=None, metadata=None):
    return field(default_factory=lambda: default, metadata=metadata)

_RE_CHECKPOINT = re.compile(r"^checkpoint-(\d+)-epoch-(\d+)$")

def get_last_checkpoint(folder):
    content = os.listdir(folder)
    checkpoints = [
        path
        for path in content
        if _RE_CHECKPOINT.search(path) is not None and os.path.isdir(os.path.join(folder, path))
    ]
    if len(checkpoints) == 0:
        return
    return os.path.join(folder, max(checkpoints, key=lambda x: int(_RE_CHECKPOINT.search(x).groups()[0])))

def sorted_checkpoints(output_dir=None, checkpoint_prefix="checkpoint") -> List[str]:
    """Helper function to sort saved checkpoints from oldest to newest."""
    ordering_and_checkpoint_path = []

    glob_checkpoints = [str(x) for x in Path(output_dir).glob(f"{checkpoint_prefix}-*") if os.path.isdir(x)]

    for path in glob_checkpoints:
        regex_match = re.match(f".*{checkpoint_prefix}-([0-9]+)", path)
        if regex_match is not None and regex_match.groups() is not None:
            ordering_and_checkpoint_path.append((int(regex_match.groups()[0]), path))

    checkpoints_sorted = sorted(ordering_and_checkpoint_path)
    checkpoints_sorted = [checkpoint[1] for checkpoint in checkpoints_sorted]
    return checkpoints_sorted

def rotate_checkpoints(save_total_limit=None, output_dir=None, checkpoint_prefix="checkpoint") -> None:
    """Helper function to delete old checkpoints."""
    if save_total_limit is None or save_total_limit <= 0:
        return
    # Check if we should delete older checkpoint(s)
    checkpoints_sorted = sorted_checkpoints(output_dir=output_dir, checkpoint_prefix=checkpoint_prefix)
    if len(checkpoints_sorted) <= save_total_limit:
        return

    number_of_checkpoints_to_delete = max(0, len(checkpoints_sorted) - save_total_limit)
    checkpoints_to_be_deleted = checkpoints_sorted[:number_of_checkpoints_to_delete]
    for checkpoint in checkpoints_to_be_deleted:
        logger.info(f"Deleting older checkpoint [{checkpoint}] due to args.save_total_limit")
        shutil.rmtree(checkpoint, ignore_errors=True)

def log_metric(
    accelerator,
    metrics: Dict,
    train_time: float,
    step: int,
    epoch: int,
    learning_rate: float = None,
    prefix: str = "train",
):
    """Helper function to log all training/evaluation metrics with the correct prefixes and styling."""
    log_metrics = {}
    for k, v in metrics.items():
        log_metrics[f"{prefix}/{k}"] = v
    log_metrics[f"{prefix}/time"] = train_time
    log_metrics[f"{prefix}/epoch"] = epoch
    if learning_rate is not None:
        log_metrics[f"{prefix}/learning_rate"] = learning_rate
    accelerator.log(log_metrics, step=step)

def log_pred(
    accelerator,
    pred_descriptions: List[str],
    pred_prompts: List[str],
    transcriptions: List[str],
    audios: List[torch.Tensor],
    sampling_rate: int,
    step: int,
    prefix: str = "eval",
    num_lines: int = 200000,
):
    """Helper function to log target/predicted transcriptions to weights and biases (wandb)."""
    if accelerator.is_main_process:
        wandb_tracker = accelerator.get_tracker("wandb")
        # pretty name for current step: step 50000 -> step 50k
        cur_step_pretty = f"{int(step // 1000)}k" if step > 1000 else step
        prefix_pretty = prefix.replace("/", "-")

        # convert str data to a wandb compatible format
        str_data = [[pred_descriptions[i], pred_prompts[i], transcriptions[i]] for i in range(len(pred_descriptions))]
        # log as a table with the appropriate headers
        wandb_tracker.log_table(
            table_name=f"predictions/{prefix_pretty}-step-{cur_step_pretty}",
            columns=["Target descriptions", "Target prompts", "Predicted transcriptions"],
            data=str_data[:num_lines],
            step=step,
            commit=False,
        )
        
        # wandb can only loads 100 audios per step
        wandb_tracker.log({
                "Speech samples": [
                    Audio(
                        audio,
                        caption=f"{pred_prompts[i]} --- DESCRIPTION: {pred_descriptions[i]}",
                        sample_rate=sampling_rate,
                    )
                    for (i, audio) in enumerate(audios[:min(len(audios), 100)])
                ]},
                step=step)

#### ARGUMENTS

class StableSpeechTrainer(Seq2SeqTrainer):
    def _pad_tensors_to_max_len(self, tensor, max_length):
        if self.model.config.pad_token_id is not None:
            pad_token_id = self.model.config.pad_token_id
        else:
            raise ValueError("Pad_token_id must be set in the configuration of the model, in order to pad tensors")

        padded_tensor = pad_token_id * torch.ones(
            (tensor.shape[0], max_length, tensor.shape[2]), dtype=tensor.dtype, device=tensor.device
        )
        padded_tensor[:, : tensor.shape[1]] = tensor
        return padded_tensor


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """
    # TODO: pretrain from scratch
    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    feature_extractor_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained feature extractor name or path if not the same as model_name"}
    )
    description_tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained description tokenizer name or path if not the same as model_name"}
    )
    prompt_tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained prompt tokenizer name or path if not the same as description_tokenizer_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    pad_token_id: int = field(
        default=None,
        metadata={"help": "If specified, change the model pad token id."},
    )
    decoder_start_token_id: int = field(
        default=None,
        metadata={"help": "If specified, change the model decoder start token id."},
    )
    freeze_text_encoder: bool = field(
        default=False,
        metadata={"help": "Whether to freeze the text encoder."},
    )
    do_sample: bool = field(
        default=False,
        metadata={"help": "Whether to do sampling or greedy decoding."},
    )
    max_length: int = field(
        default=400, # TODO
        metadata={"help": "Whether to do sampling or greedy decoding."},
    )
    


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.

    Using `HfArgumentParser` we can turn this class
    into argparse arguments to be able to specify them on
    the command line.
    """

    train_dataset_name: str = field(
        default=None,
        metadata={
            "help": "The name of the training dataset to use (via the datasets library). Load and combine "
            "multiple datasets by separating dataset ids by a '+' symbol. For example, to load and combine "
            " librispeech and common voice, set `train_dataset_name='librispeech_asr+common_voice'`."
        },
    )
    train_dataset_config_name: Optional[str] = field(
        default=None,
        metadata={
            "help": "The configuration name of the training dataset to use (via the datasets library). Load and combine "
            "multiple datasets by separating dataset configs by a '+' symbol."
        },
    )
    train_split_name: str = field(
        default="train",
        metadata={
            "help": ("The name of the training data set split to use (via the datasets library). Defaults to 'train'")
        },
    )
    train_dataset_samples: str = field(
        default=None,
        metadata={
            "help": "Number of samples in the training data. Load and combine "
            "multiple datasets by separating dataset samples by a '+' symbol."
        },
    )
    train_metadata_dataset_name: str = field(
        default=None,
        metadata={
            "help": "The name of the metadata training dataset to use (via the datasets library). Load and combine "
            "multiple datasets by separating dataset ids by a '+' symbol. For example, to load and combine "
            " librispeech and common voice, set `train_dataset_name='librispeech_asr+common_voice'`."
        },
    )
    eval_dataset_name: str = field(
        default=None,
        metadata={
            "help": "The name of the evaluation dataset to use (via the datasets library). Defaults to the training dataset name if unspecified."
        },
    )
    eval_dataset_config_name: Optional[str] = field(
        default=None,
        metadata={
            "help": "The configuration name of the evaluation dataset to use (via the datasets library). Defaults to the training dataset config name if unspecified"
        },
    )
    eval_split_name: str = field(
        default="test",
        metadata={
            "help": "The name of the evaluation data set split to use (via the datasets library). Defaults to 'test'"
        },
    )
    eval_metadata_dataset_name: str = field(
        default=None,
        metadata={
            "help": "The name of the metadata training dataset to use (via the datasets library). Load and combine "
            "multiple datasets by separating dataset ids by a '+' symbol. For example, to load and combine "
            " librispeech and common voice, set `train_dataset_name='librispeech_asr+common_voice'`."
        },
    )
    target_audio_column_name: str = field( # TODO
        default="audio",
        metadata={"help": "The name of the dataset column containing the target audio data. Defaults to 'audio'"},
    )
    description_column_name: str = field( #TODO
        default=None,
        metadata={"help": "The name of the dataset column containing the text data. Defaults to 'None'."},
    )
    prompt_column_name: str = field( #TODO
        default=None,
        metadata={"help": "The name of the dataset column containing the text data. Defaults to 'None'."},
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached preprocessed datasets or not."}
    )
    preprocessing_num_workers: Optional[int] = field(
        default=None,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of training examples to this "
                "value if set."
            )
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": (
                "For debugging purposes or quicker training, truncate the number of validation examples to this "
                "value if set."
            )
        },
    )
    max_duration_in_seconds: float = field(
        default=35.0,
        metadata={
            "help": (
                "Filter audio files that are longer than `max_duration_in_seconds` seconds to"
                " 'max_duration_in_seconds`"
            )
        },
    )
    min_duration_in_seconds: float = field(
        default=0.0, metadata={"help": "Filter audio files that are shorter than `min_duration_in_seconds` seconds"}
    )
    preprocessing_only: bool = field(
        default=False,
        metadata={
            "help": (
                "Whether to only do data preprocessing and skip training. This is especially useful when data"
                " preprocessing errors out in distributed training due to timeout. In this case, one should run the"
                " preprocessing in a non-distributed setup with `preprocessing_only=True` so that the cached datasets"
                " can consequently be loaded in distributed training"
            )
        },
    )
    token: str = field(
        default=None,
        metadata={
            "help": (
                "The token to use as HTTP bearer authorization for remote files. If not specified, will use the token "
                "generated when running `huggingface-cli login` (stored in `~/.huggingface`)."
            )
        },
    )
    use_auth_token: bool = field(
        default=None,
        metadata={
            "help": "The `use_auth_token` argument is deprecated and will be removed in v4.34. Please use `token` instead."
        },
    )
    trust_remote_code: bool = field(
        default=False,
        metadata={
            "help": (
                "Whether or not to allow for custom models defined on the Hub in their own modeling files. This option "
                "should only be set to `True` for repositories you trust and in which you have read the code, as it will "
                "execute code present on the Hub on your local machine."
            )
        },
    )
    add_audio_samples_to_wandb: bool = field(
        default=False,
        metadata={
            "help": "If set and if `wandb` in args.report_to, will add generated audio samples to wandb logs."
        }
    )
    id_column_name: str = field(
        default=None,
        metadata={
            "help": "id column name."
        }
    )
    wandb_project: str = field(
        default="stable-speech",
        metadata={"help": "The name of the wandb project."},
    )
    
@dataclass
class StableSpeechTrainingArguments(Seq2SeqTrainingArguments):
    dtype: Optional[str] = field(
        default="float32",
        metadata={
            "help": (
                "The data type (dtype) in which to run training. One of `float32` (full-precision), "
                "`float16` or `bfloat16` (both half-precision)."
            )
        },
    )
    audio_encode_per_device_eval_batch_size: int = field(
        default=8,
        metadata={
            "help": (
                "TODO"
            )
        },
    )
    
@dataclass
class DataCollatorEncodecWithPadding:
    """
    Data collator that will dynamically pad the inputs received to the longest sequence in the batch.
    """

    feature_extractor: AutoFeatureExtractor
    feature_extractor_input_name: Optional[str] = "input_values"

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        # split inputs and labels since they have to be of different lengths and need
        # different padding methods
        audios = [torch.tensor(feature["labels"]).squeeze() for feature in features]
        len_audio = [len(audio) for audio in audios]
        
        input_features = {self.feature_extractor_input_name: audios}
        batch = self.feature_extractor.pad(input_features, return_tensors="pt", padding="longest", return_attention_mask=True)
        batch[self.feature_extractor_input_name] = batch[self.feature_extractor_input_name].unsqueeze(1) # add mono-channel 
        batch["padding_mask"] = batch.pop("attention_mask")   
        batch["len_audio"] = torch.tensor(len_audio).unsqueeze(1)
        return batch

    
@dataclass
class DataCollatorStableSpeechWithPadding:
    """
    Data collator that will dynamically pad the inputs received.
    Args:
        prompt_tokenizer (:class:`~transformers.AutoTokenizer`)
            The prompt_tokenizer used for proccessing the data.
        description_tokenizer (:class:`~transformers.AutoTokenizer`)
            The description_tokenizer used for proccessing the data.
        audio_feature_extractor (:class:`~transformers.AutoFeatureExtractor`)
            The audio_feature_extractor used for proccessing the data.
        padding (:obj:`bool`, :obj:`str` or :class:`~transformers.tokenization_utils_base.PaddingStrategy`, `optional`, defaults to :obj:`True`):
            Select a strategy to pad the returned sequences (according to the model's padding side and padding index)
            among:
            * :obj:`True` or :obj:`'longest'`: Pad to the longest sequence in the batch (or no padding if only a single
              sequence if provided).
            * :obj:`'max_length'`: Pad to a maximum length specified with the argument :obj:`max_length` or to the
              maximum acceptable input length for the model if that argument is not provided.
            * :obj:`False` or :obj:`'do_not_pad'` (default): No padding (i.e., can output a batch with sequences of
              different lengths).
        pad_to_multiple_of (:obj:`int`, `optional`):
            If set will pad the sequence to a multiple of the provided value.
            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
    """

    prompt_tokenizer: AutoTokenizer
    description_tokenizer: AutoTokenizer
    audio_feature_extractor: AutoFeatureExtractor
    feature_extractor_input_name: Optional[str] = "input_values"
    padding: Union[bool, str] = "longest"
    pad_to_multiple_of: Optional[int] = None

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        # split inputs and labels since they have to be of different lengths and need
        # different padding methods
        
        
        labels = [torch.tensor(feature["labels"]).transpose(0,1) for feature in features]
        # (bsz, seq_len, num_codebooks)
        labels = torch.nn.utils.rnn.pad_sequence(labels,batch_first=True,padding_value=-100)
        
        input_ids = [{"input_ids": feature["input_ids"]} for feature in features]
        input_ids = self.description_tokenizer.pad(input_ids, return_tensors="pt", padding=self.padding, pad_to_multiple_of=self.pad_to_multiple_of)

        batch= {"labels":labels, **input_ids}

        prompt_input_ids = [{"input_ids": feature["prompt_input_ids"]} for feature in features]
        prompt_input_ids = self.prompt_tokenizer.pad(prompt_input_ids, return_tensors="pt", padding=self.padding, pad_to_multiple_of=self.pad_to_multiple_of)
        
        # TODO: check it's been padded on the left
        batch["prompt_input_ids"] = prompt_input_ids["input_ids"]
        if "attention_mask" in prompt_input_ids:
            batch["prompt_attention_mask"] = prompt_input_ids["attention_mask"]
        
        if self.feature_extractor_input_name in features[0]:
            # TODO: verify that it works
            input_values = [{self.feature_extractor_input_name: feature[self.feature_extractor_input_name]} for feature in features]
            input_values = self.feature_extractor.pad(input_values, return_tensors="pt")
            
            batch[self.feature_extractor_input_name: input_values]
        
        return batch
    

def convert_dataset_str_to_list(
    dataset_names,
    dataset_config_names,
    metadata_dataset_names=None,
    splits=None,
    dataset_samples=None,
    default_split="train",
):
    if isinstance(dataset_names, str):
        dataset_names = dataset_names.split("+")
        dataset_config_names = dataset_config_names.split("+")
        splits = splits.split("+") if splits is not None else None
        dataset_samples = dataset_samples.split("+") if dataset_samples is not None else None
        metadata_dataset_names = metadata_dataset_names.split("+") if metadata_dataset_names is not None else None

    # basic checks to ensure we've got the right number of datasets/configs/splits/columns/probs
    if len(dataset_names) != len(dataset_config_names):
        raise ValueError(
            f"Ensure one config is passed for each dataset, got {len(dataset_names)} datasets and"
            f" {len(dataset_config_names)} configs."
        )

    if splits is not None and len(splits) != len(dataset_names):
        raise ValueError(
            f"Ensure one split is passed for each dataset, got {len(dataset_names)} datasets and {len(splits)} splits."
        )

    if metadata_dataset_names is not None and len(metadata_dataset_names) != len(dataset_names):
        raise ValueError(
            f"Ensure one metadata dataset is passed for each dataset, got {len(dataset_names)} datasets and {len(metadata_dataset_names)} metadata datasets."
        )

    if dataset_samples is not None:
        if len(dataset_samples) != len(dataset_names):
            raise ValueError(
                f"Ensure one sample is passed for each dataset, got {len(dataset_names)} datasets and "
                f"{len(dataset_samples)} samples."
            )
        dataset_samples = [float(ds_sample) for ds_sample in dataset_samples]
    else:
        dataset_samples = [None] * len(dataset_names)

    splits = splits if splits is not None else [default_split for _ in range(len(dataset_names))]

    dataset_names_dict = []
    for i, ds_name in enumerate(dataset_names):
        dataset_names_dict.append(
            {
                "name": ds_name,
                "config": dataset_config_names[i],
                "split": splits[i],
                "metadata_dataset_name": metadata_dataset_names[i],
                "samples": dataset_samples[i],
            }
        )
    return dataset_names_dict


def load_multiple_datasets(
    accelerator: Accelerator,
    dataset_names: Union[List, str],
    dataset_config_names: Union[List, str],
    metadata_dataset_names: Optional[str]=None,
    splits: Optional[Union[List, str]] = None,
    label_column_names: Optional[List] = None,
    stopping_strategy: Optional[str] = "first_exhausted",
    dataset_samples: Optional[Union[List, np.array]] = None,
    streaming: Optional[bool] = False,
    seed: Optional[int] = None,
    id_column_name: Optional[str] = None,
    columns_to_keep: Optional[Set[str]] = None,
    **kwargs,
) -> Union[Dataset, IterableDataset]:
    dataset_names_dict = convert_dataset_str_to_list(
        dataset_names, dataset_config_names, metadata_dataset_names, splits, label_column_names, dataset_samples
    )

    if dataset_samples is not None:
        dataset_samples = [ds_dict["samples"] for ds_dict in dataset_names_dict]
        probabilities = np.array(dataset_samples) / np.sum(dataset_samples)
    else:
        probabilities = None

    all_datasets = []
    # iterate over the datasets we want to interleave
    for dataset_dict in tqdm(dataset_names_dict, desc="Combining datasets..."):
        with accelerator.main_process_first():
            dataset = load_dataset(
                dataset_dict["name"],
                dataset_dict["config"],
                split=dataset_dict["split"],
                streaming=streaming,
                **kwargs,
            )
            dataset_features = dataset.features.keys()
            
            metadata_dataset_name = dataset_dict["metadata_dataset_name"]
            if metadata_dataset_name is not None:
                metadata_dataset = load_dataset(
                    metadata_dataset_name,
                    dataset_dict["config"],
                    split=dataset_dict["split"],
                    streaming=streaming,
                    **kwargs,
                )
                        
                if id_column_name is not None and id_column_name not in dataset.column_names:
                    raise ValueError(
                        f"id_column_name={id_column_name} but has not been found in the dataset columns"
                        f"- one of {', '.join(list(dataset.column_names))}."
                        )
                if id_column_name is not None and id_column_name not in metadata_dataset.column_names:
                    raise ValueError(
                        f"id_column_name={id_column_name} but has not been found in the metadata dataset columns"
                        f"- one of {', '.join(list(metadata_dataset.column_names))}."
                        )
                elif id_column_name is not None:
                    metadata_dataset = metadata_dataset.rename_column(id_column_name, f"metadata_{id_column_name}")
                    
                
                metadata_columns_to_remove = set(metadata_dataset.column_names).intersection(set(dataset.column_names))
                metadata_dataset = metadata_dataset.remove_columns(metadata_columns_to_remove)
                dataset = concatenate_datasets([dataset, metadata_dataset], axis=1)
                
                if id_column_name is not None:
                    if len(dataset.filter(lambda id1, id2: id1!=id2, input_columns=[id_column_name, f"metadata_{id_column_name}"])) != 0:
                        raise ValueError(f"Concatenate didn't work. Some ids don't correspond on dataset {dataset_dict['name']}")
                    
                dataset_features = dataset.features.keys()

            if columns_to_keep is not None:
                dataset = dataset.remove_columns(set(dataset_features - columns_to_keep))
        all_datasets.append(dataset)

    if len(all_datasets) == 1:
        # we have a single dataset so just return it as is
        return all_datasets[0]

    if streaming:
        interleaved_dataset = interleave_datasets(
            all_datasets,
            stopping_strategy=stopping_strategy,
            probabilities=probabilities,
            seed=seed,
        )
    else:
        with accelerator.main_process_first():
            interleaved_dataset = concatenate_datasets(all_datasets)

    return interleaved_dataset

    
    
def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, StableSpeechTrainingArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args = parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
    else:
        model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your Python/PyTorch versions.
    send_example_telemetry("run_stable_speech", model_args, data_args)
    
    if training_args.dtype == "float16":
        mixed_precision = "fp16"
    elif training_args.dtype == "bfloat16":
        mixed_precision = "bf16"
    else:
        mixed_precision = "no"

    accelerator = Accelerator(
        gradient_accumulation_steps=training_args.gradient_accumulation_steps,
        mixed_precision=mixed_precision,
        log_with=training_args.report_to,
        project_dir=training_args.output_dir,
    )
    
    accelerator.init_trackers(project_name=data_args.wandb_project)
    
    
    # Detecting last checkpoint and eventually continue from last checkpoint
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )
    

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.setLevel(logging.INFO if accelerator.is_main_process else logging.WARN)

    # Log a small summary on each proces
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}, "
        f"distributed training: {training_args.parallel_mode.value == 'distributed'}, 16-bits training: {training_args.fp16}"
    )

    # Set the verbosity to info of the Transformers logger (on main process only)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()


    logger.info("Training/evaluation parameters %s", training_args)

    # Set seed before initializing model.
    set_seed(training_args.seed)

    # 1. First, let's load the dataset
    raw_datasets = DatasetDict()
    num_workers = data_args.preprocessing_num_workers
    
    columns_to_keep = {
        "target_audio_column_name": data_args.target_audio_column_name,
        "prompt_column_name": data_args.prompt_column_name
    }
    if data_args.description_column_name is not None:
        columns_to_keep["description_column_nam"] = data_args.description_column_name
        
    if training_args.do_train:
        raw_datasets["train"] = load_multiple_datasets(
            accelerator,
            data_args.train_dataset_name,
            data_args.train_dataset_config_name,
            metadata_dataset_names=data_args.train_metadata_dataset_name,
            splits=data_args.train_split_name,
            dataset_samples=data_args.train_dataset_samples,
            seed=training_args.seed,
            cache_dir=model_args.cache_dir,
            num_proc=data_args.preprocessing_num_workers,
            id_column_name=data_args.id_column_name,
            columns_to_keep=columns_to_keep.values(),
            # streaming=data_args.streaming, TODO(SG): optionally enable streaming mode
        )
        
        for key in columns_to_keep:
            if columns_to_keep[key] not in raw_datasets["train"].column_names:
                raise ValueError(
                    f"--{key} '{columns_to_keep[key]}' not found in dataset '{data_args.train_dataset_name}'."
                    f" Make sure to set `--{key}` to the correct audio column - one of"
                    f" {', '.join(raw_datasets['train'].column_names)}."
                )        

        if data_args.max_train_samples is not None:
            raw_datasets["train"] = raw_datasets["train"].select(range(data_args.max_train_samples))

    if training_args.do_eval:
        raw_datasets["eval"] = load_multiple_datasets(
            accelerator,
            data_args.eval_dataset_name if data_args.eval_dataset_name else data_args.train_dataset_name,
            data_args.eval_dataset_config_name if data_args.eval_dataset_config_name else data_args.train_dataset_config_name,
            metadata_dataset_names=data_args.eval_metadata_dataset_name,
            splits=data_args.eval_split_name,
            cache_dir=model_args.cache_dir,
            num_proc=data_args.preprocessing_num_workers,
            id_column_name=data_args.id_column_name,
            columns_to_keep=columns_to_keep.values(),
            # streaming=data_args.streaming, TODO(SG): optionally enable streaming mode
        )

        if data_args.max_eval_samples is not None:
            raw_datasets["eval"] = raw_datasets["eval"].select(range(data_args.max_eval_samples))


    # 2. Next, let's load the config as we might need it to create
    # load config TODO: add the option to create the config from scratch
    config = StableSpeechConfig.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        token=data_args.token,
        trust_remote_code=data_args.trust_remote_code,
    )
    
    # update pad token id and decoder_start_token_id
    # TODO: verify if this makes sense, maybe should do it for model.decoder
    config.update({
        "pad_token_id": model_args.pad_token_id if model_args.pad_token_id is not None else model.config.pad_token_id,
        "decoder_start_token_id": model_args.decoder_start_token_id if model_args.decoder_start_token_id is not None else model.config.decoder_start_token_id,
    })

    # 3. Now we can instantiate the feature extractor, tokenizers and model
    # Note for distributed training, the .from_pretrained methods guarantee that only
    # one local process can concurrently download model & vocab.
    
    # load feature extractor
    feature_extractor = AutoFeatureExtractor.from_pretrained(
        model_args.feature_extractor_name or model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        token=data_args.token,
        trust_remote_code=data_args.trust_remote_code,
    )
    
    # load prompt tokenizer
    prompt_tokenizer = AutoTokenizer.from_pretrained(
        model_args.prompt_tokenizer_name or model_args.description_tokenizer_name or model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        token=data_args.token,
        trust_remote_code=data_args.trust_remote_code,
        use_fast=model_args.use_fast_tokenizer,
        padding_side="left", # prompt has to be padded on the left bc it's preprend to codebooks hidden states
    )
        
    # load description tokenizer
    description_tokenizer = AutoTokenizer.from_pretrained(
        model_args.description_tokenizer_name or model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        token=data_args.token,
        trust_remote_code=data_args.trust_remote_code,
        use_fast=model_args.use_fast_tokenizer,
    )
    
    if model_args.use_fast_tokenizer:
        logger.warning("Disabling fast tokenizer warning: https://github.com/huggingface/transformers/blob/main/src/transformers/tokenization_utils_base.py#L3231-L3235")
        prompt_tokenizer.deprecation_warnings["Asking-to-pad-a-fast-tokenizer"] = True
        description_tokenizer.deprecation_warnings["Asking-to-pad-a-fast-tokenizer"] = True

    
    # create model + TODO: not from_pretrained probably
    model = StableSpeechForConditionalGeneration.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        config=config,
        token=data_args.token,
        trust_remote_code=data_args.trust_remote_code,
    )
    
    
    # 4. Now we preprocess the datasets including loading the audio, resampling and normalization
    # Thankfully, `datasets` takes care of automatically loading and resampling the audio,
    # so that we just need to set the correct target sampling rate and normalize the input
    # via the `feature_extractor`
    
    # derive max & min input length for sample rate & max duration
    sampling_rate = feature_extractor.sampling_rate
    max_target_length = data_args.max_duration_in_seconds * sampling_rate
    min_target_length = data_args.min_duration_in_seconds * sampling_rate
    target_audio_column_name = data_args.target_audio_column_name
    description_column_name = data_args.description_column_name
    prompt_column_name = data_args.prompt_column_name
    feature_extractor_input_name = feature_extractor.model_input_names[0]
    audio_encoder_pad_token_id = config.decoder.pad_token_id
    audio_encoder_eos_token_id = config.decoder.eos_token_id
    audio_encoder_bos_token_id = model.generation_config.decoder_start_token_id
    max_length = model.generation_config.max_length
    num_codebooks = model.decoder.config.num_codebooks
    
    # resample target audio
    raw_datasets = raw_datasets.cast_column(
        target_audio_column_name, datasets.features.Audio(sampling_rate=sampling_rate)
    )
    

    # Preprocessing the datasets.
    # We need to read the audio files as arrays and tokenize the texts.
    def pass_through_processors(batch):
        # load audio
        if description_column_name is not None:
            text = batch[description_column_name]
            batch["input_ids"] = description_tokenizer(text.strip())["input_ids"]
            
        if prompt_column_name is not None:
            text = batch[prompt_column_name]
            batch["prompt_input_ids"] = prompt_tokenizer(text.strip())["input_ids"]

        # load audio
        target_sample = batch[target_audio_column_name]
        labels = feature_extractor(target_sample["array"], sampling_rate=target_sample["sampling_rate"])
        batch["labels"] = labels["input_values"]
        
        # take length of raw audio waveform
        batch["target_length"] = len(target_sample["array"].squeeze())
        return batch

    with accelerator.main_process_first():
        vectorized_datasets = raw_datasets.map(
            pass_through_processors,
            remove_columns=next(iter(raw_datasets.values())).column_names,
            num_proc=num_workers,
            desc="preprocess datasets",
        )

        def is_audio_in_length_range(length):
            return length > min_target_length and length < max_target_length

        # filter data that is shorter than min_target_length
        vectorized_datasets = vectorized_datasets.filter(
            is_audio_in_length_range,
            num_proc=num_workers,
            input_columns=["target_length"],
        )


    # 5. Now we encode the audio labels with encodec.
    # We use Accelerate to perform distributed inference

    logger.info("*** Encode target audio with encodec ***")
    
    # no need to prepare audio_decoder because used for inference without mixed precision
    # see: https://huggingface.co/docs/accelerate/main/en/package_reference/accelerator#accelerate.Accelerator.prepare
    audio_decoder = model.audio_encoder

    encoder_data_collator = DataCollatorEncodecWithPadding(feature_extractor, feature_extractor_input_name)

    def apply_audio_decoder(batch):
        len_audio = batch.pop("len_audio")
        audio_decoder.to(batch["input_values"].device).eval()
        with torch.no_grad():
            labels = audio_decoder.encode(**batch)["audio_codes"]
        output = {}
        output["len_audio"] = len_audio
        # (1, bsz, codebooks, seq_len) -> (bsz, seq_len, codebooks)
        output["labels"] = labels.squeeze(0).transpose(1,2)
        output["ratio"] = torch.ones_like(len_audio) * labels.shape[-1] / len_audio.max() 
        return output

    for split in vectorized_datasets:
        data_loader = DataLoader(
            vectorized_datasets[split],
            batch_size=training_args.audio_encode_per_device_eval_batch_size,
            collate_fn=encoder_data_collator,
            num_workers=training_args.dataloader_num_workers,
            pin_memory=True,
        )
        data_loader = accelerator.prepare(data_loader)        
        
        all_generated_labels = []
        all_ratios = []
        all_lens = []
        for batch in tqdm(data_loader, disable=not accelerator.is_local_main_process):
            generate_labels = apply_audio_decoder(batch)
            generate_labels = accelerator.pad_across_processes(generate_labels, dim=1, pad_index=0)
            generate_labels = accelerator.gather_for_metrics(generate_labels)
            
            all_generated_labels.extend(generate_labels["labels"].cpu())
            all_ratios.extend(generate_labels["ratio"].cpu())
            all_lens.extend(generate_labels["len_audio"].cpu())
            
        # (1, codebooks, seq_len) where seq_len=1
        eos_labels = torch.ones((1, num_codebooks, 1)) * audio_encoder_eos_token_id
        bos_labels = torch.ones((1, num_codebooks, 1)) * audio_encoder_bos_token_id
            
        def postprocess_dataset(input_ids, prompt_input_ids, idx):
            # (1, codebooks, seq_len)
            labels = all_generated_labels[idx].transpose(0,1).unsqueeze(0)
            len_ = int(all_ratios[idx] * all_lens[idx])
            labels = labels[:, :, :len_]
            
            # labels = labels[:, :, :(len_)%10+20] # TODO: change
            
            # add bos
            labels = torch.cat([bos_labels, labels], dim=-1)
            
            labels, delay_pattern_mask = build_delay_pattern_mask(labels, 
                                                    bos_token_id=audio_encoder_bos_token_id,
                                                    pad_token_id=audio_encoder_eos_token_id,
                                                    max_length=labels.shape[-1] + num_codebooks,
                                                    num_codebooks=num_codebooks)
            
            
            # the first ids of the delay pattern mask are precisely labels, we use the rest of the labels mask
            # to take care of EOS
            # we want labels to look like this:
            #  - [B, a, b, E, E, E, E]
            #  - [B, B, c, d, E, E, E]
            #  - [B, B, B, e, f, E, E]
            #  - [B, B, B, B, g, h, E] 
            labels = torch.where(delay_pattern_mask==-1, audio_encoder_eos_token_id, delay_pattern_mask)
                        
            # the first timestamp is associated to a row full of BOS, let's get rid of it
            # we also remove the last timestampts (full of PAD)
            output = {"labels": labels[:, 1:].cpu()}
            output["input_ids"] = input_ids
            output["prompt_input_ids"] = prompt_input_ids
            return output

        # TODO: done multiple times, how to deal with it.
        with accelerator.main_process_first():
            vectorized_datasets[split] = vectorized_datasets[split].map(
                postprocess_dataset,
                num_proc=num_workers,
                input_columns=["input_ids", "prompt_input_ids"],
                desc="Postprocessing labeling",
                with_indices=True,
            )

            
    accelerator.free_memory()
    del generate_labels

    

    # for large datasets it is advised to run the preprocessing on a
    # single machine first with ``args.preprocessing_only`` since there will mostly likely
    # be a timeout when running the script in distributed mode.
    # In a second step ``args.preprocessing_only`` can then be set to `False` to load the
    # cached dataset
    if data_args.preprocessing_only:
        # TODO: save to disk in this step instead of something else ??
        logger.info(f"Data preprocessing finished. Files cached at {vectorized_datasets.cache_files}")
        return
    
    # Now save everything to be able to create a single processor later
    # make sure all processes wait until data is saved
    with accelerator.main_process_first():
        # only the main process saves them
        if accelerator.is_main_process:
            # save feature extractor, tokenizer and config
            if model_args.prompt_tokenizer_name is None and model_args.description_tokenizer_name or (model_args.prompt_tokenizer_name==model_args.description_tokenizer_name):
                prompt_tokenizer.save_pretrained(training_args.output_dir)
            else:
                logger.warning("Prompt tokenizer ('{model_args.prompt_tokenizer_name}') and description tokenizer ('{model_args.description_tokenizer_name}') are not the same. Saving only the prompt tokenizer.")
                prompt_tokenizer.save_pretrained(training_args.output_dir)
            
            feature_extractor.save_pretrained(training_args.output_dir)
            config.save_pretrained(training_args.output_dir)
            

    
    # 6. Next, we can prepare the training.
    
    # enable gradient checkpointing if necessary
    if training_args.gradient_checkpointing:
        model.gradient_checkpointing_enable()

    
    # Let's use word CLAP similary and WER metrics as our evaluation metrics,

    # Define evaluation metrics during training, *i.e.* CLAP similarity TODO: allow using another CLAP
    clap = AutoModel.from_pretrained("laion/larger_clap_music_and_speech")
    clap_processor = AutoProcessor.from_pretrained("laion/larger_clap_music_and_speech")
    metric = evaluate.load("wer")
    
    
    def clap_similarity(texts, audios, device):
        clap_inputs = clap_processor(text=texts, audios=audios, padding=True, return_tensors="pt").to(device)
        clap.to(device)
        text_features = clap.get_text_features(clap_inputs["input_ids"], attention_mask=clap_inputs.get("attention_mask", None))
        audio_features = clap.get_audio_features(clap_inputs["input_features"])
        
        cosine_sim = torch.nn.functional.cosine_similarity(audio_features, text_features, dim=1, eps=1e-8)
        
        return cosine_sim.mean()
    
    def wer(prompts, audios, device):
        asr_pipeline = pipeline(model="distil-whisper/distil-large-v2", device=device)
        transcriptions = asr_pipeline([{'raw': audio, 'sampling_rate': sampling_rate} for audio in audios])
        
        word_error = 100 * metric.compute(predictions=[t["text"].lower() for t in transcriptions], references=[t.lower() for t in prompts])
        
        return word_error, [t["text"] for t in transcriptions]
        
    
    eval_methods = {"clap": clap_similarity, "wer": wer}

    def compute_metrics(audios, descriptions, prompts, device="cpu"):
        input_ids = descriptions
        input_ids[input_ids==-100] = description_tokenizer.pad_token_id
        texts = description_tokenizer.batch_decode(input_ids, skip_special_tokens=True)
        prompts = prompt_tokenizer.batch_decode(prompts, skip_special_tokens=True)
        audios = [a.cpu().numpy() for a in audios]
        results = {
            "clap": eval_methods["clap"](texts, audios, device)
        }
        word_error, transcriptions = eval_methods["wer"](prompts, audios, device)
        results["wer"] = word_error

        return results, texts, prompts, audios, transcriptions
    
    # Define Training Schedule
    # Store some constants
    per_device_train_batch_size = int(training_args.per_device_train_batch_size)
    train_batch_size = per_device_train_batch_size * accelerator.num_processes
    gradient_accumulation_steps = int(training_args.gradient_accumulation_steps)
    per_device_eval_batch_size = int(training_args.per_device_eval_batch_size)
    
    
    if training_args.max_steps < 0:
        num_epochs = int(training_args.num_train_epochs)
        steps_per_epoch = len(vectorized_datasets["train"]) // (train_batch_size * gradient_accumulation_steps)
        total_train_steps = steps_per_epoch * num_epochs
    elif training_args.max_steps > 0:
        logger.info("max_steps is given, it will override any value given in num_train_epochs")
        total_train_steps = int(training_args.max_steps)
        # Setting a very large number of epochs so we go as many times as necessary over the iterator.
        num_epochs = sys.maxsize
        steps_per_epoch = total_train_steps

    if training_args.eval_steps is None:
        logger.info(
            f"eval_steps is not set, evaluating at the end of each epoch"
        )
        eval_steps = steps_per_epoch
    else:
        eval_steps = training_args.eval_steps
        
    # Define optimizer, LR scheduler, collator
    optimizer = torch.optim.AdamW(
        params=model.parameters(),
        lr=training_args.learning_rate,
        betas=(training_args.adam_beta1, training_args.adam_beta2),
        eps=training_args.adam_epsilon,
    )

    # LR scheduler gets stepped by `num_processes` each time -> account for this in warmup / total steps
    lr_scheduler = get_scheduler(
        name=training_args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=training_args.warmup_steps * accelerator.num_processes,
        num_training_steps=total_train_steps * accelerator.num_processes,
    )

    # Instantiate custom data collator
    data_collator = DataCollatorStableSpeechWithPadding(
        audio_feature_extractor=feature_extractor, feature_extractor_input_name=feature_extractor_input_name, prompt_tokenizer=prompt_tokenizer, description_tokenizer=description_tokenizer
    )
    
    # Freeze Encoders
    model.freeze_encoders(model_args.freeze_text_encoder)
    
    # Prepare everything with accelerate
    model, optimizer, lr_scheduler = accelerator.prepare(model, optimizer, lr_scheduler)
        

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {total_train_steps * train_batch_size * gradient_accumulation_steps}")
    logger.info("  Instantaneous batch size per device =" f" {training_args.per_device_train_batch_size}")
    logger.info("  Gradient accumulation steps =" f" {gradient_accumulation_steps}")
    logger.info(
        f"  Total train batch size (w. parallel & distributed) = {train_batch_size * gradient_accumulation_steps}"
    )
    logger.info(f"  Total optimization steps = {total_train_steps}")

    # ======================== Training ================================
    train_time = 0
    train_start = time.time()
    steps_trained_progress_bar = tqdm(
        range(total_train_steps), desc="Train steps ... ", position=0, disable=not accelerator.is_local_main_process
    )
    continue_training = True
    epochs_trained = 0
    cur_step = 0

    checkpoint = None
    if training_args.resume_from_checkpoint is not None:
        checkpoint = training_args.resume_from_checkpoint
    elif last_checkpoint is not None:
        checkpoint = last_checkpoint
    
            
    if accelerator.is_main_process:
        if training_args.push_to_hub:
            # Retrieve of infer repo_name
            repo_name = training_args.hub_model_id
            if repo_name is None:
                repo_name = Path(training_args.output_dir).absolute().name
            # Create repo and retrieve repo_id
            repo_id = create_repo(repo_name, exist_ok=True, token=training_args.hub_token).repo_id
            # Clone repo locally
            repo = Repository(training_args.output_dir, clone_from=repo_id, token=training_args.hub_token)

            with open(os.path.join(training_args.output_dir, ".gitignore"), "w+") as gitignore:
                if "wandb" not in gitignore:
                    gitignore.write("wandb\n")
        elif training_args.output_dir is not None:
            os.makedirs(training_args.output_dir, exist_ok=True)
    accelerator.wait_for_everyone()
    

    if checkpoint is not None:
        accelerator.load_state(checkpoint)
        # Find num steps and epoch from saved state string pattern
        pattern = r"checkpoint-(\d+)-epoch-(\d+)"
        match = re.search(pattern, checkpoint)
        cur_step = int(match.group(1))
        epochs_trained = int(match.group(2))

        logger.info("  Continuing training from checkpoint, will skip to saved global_step")
        logger.info(f"  Continuing training from epoch {epochs_trained}")
        logger.info(f"  Continuing training from global step {cur_step}")

        steps_trained_progress_bar.update(cur_step)

        for epoch in range(0, epochs_trained):
            vectorized_datasets["train"] = vectorized_datasets["train"].shuffle(training_args.seed)

        if training_args.max_steps < 0:
            # we know exactly the number of steps per epoch, so can skip through the required number of batches
            resume_step = (cur_step - epochs_trained * steps_per_epoch) * gradient_accumulation_steps
        else:
            # Currently we don't know how many steps we've taken in the current epoch
            # So we just shuffle the dataset one extra time and start from a fresh epoch
            # This is "good enough" for our purposes but not fully correct
            resume_step = None
            vectorized_datasets["train"] = vectorized_datasets["train"].shuffle(training_args.seed)
    else:
        resume_step = None
        
    gen_kwargs = {
        "do_sample": model_args.do_sample,
        "max_length": model_args.max_length,
    }
    # TODO: add max_length
    
    # Define gradient update step fn
    def train_step(
        batch,
    ):
        model.train()
        outputs = model(**batch)
        # CE (data) loss
        ce_loss = outputs.loss
        # TODO: add CE per codebook 

        metrics = {"loss": ce_loss}
        return ce_loss, metrics
    
    # Define eval fn
    def eval_step(batch):
        model.eval()

        with torch.no_grad():
            outputs = model(**batch)
        # CE (data) loss
        ce_loss = outputs.loss
        metrics = {"loss": ce_loss}
        return metrics

    def generate_step(batch):
        model.eval()
        output_audios = accelerator.unwrap_model(model).generate(**batch, **gen_kwargs)
        output_audios = accelerator.pad_across_processes(output_audios, dim=1, pad_index=0)
        return output_audios


    for epoch in range(epochs_trained, num_epochs):
        vectorized_datasets["train"] = vectorized_datasets["train"].shuffle(training_args.seed)
        train_dataloader = DataLoader(
            vectorized_datasets["train"],
            collate_fn=data_collator,
            batch_size=per_device_train_batch_size,
            num_workers=training_args.dataloader_num_workers,
            pin_memory=training_args.dataloader_pin_memory,
        )
        train_dataloader = accelerator.prepare(train_dataloader)
        if hasattr(train_dataloader, "dataset") and isinstance(train_dataloader.dataset, IterableDataset):
            train_dataloader.dataset.set_epoch(epoch)

        if resume_step is not None:
            # Skip the first N batches in the dataloader when resuming from a checkpoint
            train_dataloader = accelerator.skip_first_batches(train_dataloader, resume_step)
            resume_step = None

        for batch in train_dataloader:
            with accelerator.accumulate(model):
                loss, train_metric = train_step(batch)
                accelerator.backward(loss)
                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(model.parameters(), training_args.max_grad_norm)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()


            # Check if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                steps_trained_progress_bar.update(1)
                cur_step += 1

                if cur_step % training_args.logging_steps == 0:
                    steps_trained_progress_bar.write(
                        f"Step... ({cur_step} / {total_train_steps} | Loss:"
                        f" {train_metric['loss']}, Learning Rate:"
                        f" {lr_scheduler.get_last_lr()[0]})"
                    )
                    log_metric(
                        accelerator,
                        metrics=train_metric,
                        learning_rate=lr_scheduler.get_last_lr()[0],
                        train_time=train_time + time.time() - train_start,
                        step=cur_step,
                        epoch=epoch,
                        prefix="train",
                    )

                # save checkpoint and weights after each save_steps and at the end of training
                if (cur_step % training_args.save_steps == 0) or cur_step == total_train_steps:
                    intermediate_dir = os.path.join(training_args.output_dir, f"checkpoint-{cur_step}-epoch-{epoch}")
                    accelerator.save_state(output_dir=intermediate_dir)
                    accelerator.wait_for_everyone()
                    if accelerator.is_main_process:
                        rotate_checkpoints(training_args.save_total_limit, output_dir=training_args.output_dir)

                        if cur_step == total_train_steps:
                            # un-wrap student model for save
                            model = accelerator.unwrap_model(model)
                            model.save_pretrained(training_args.output_dir)
                            # re-wrap student model for final eval
                            model = accelerator.prepare(model)

                        if training_args.push_to_hub:
                            repo.push_to_hub(
                                commit_message=f"Saving train state of step {cur_step}",
                                blocking=False,
                            )

                if training_args.do_eval and (cur_step % eval_steps == 0 or cur_step == total_train_steps):
                    train_time += time.time() - train_start
                    model.eval()
                    # ======================== Evaluating ==============================
                    eval_metrics = []
                    eval_preds = []
                    eval_descriptions = []
                    eval_prompts = []
                    eval_start = time.time()

                    validation_dataloader = DataLoader(
                        vectorized_datasets["eval"],
                        collate_fn=data_collator,
                        batch_size=per_device_eval_batch_size,
                        drop_last=False,
                        num_workers=training_args.dataloader_pin_memory,
                        pin_memory=training_args.dataloader_pin_memory,
                    )
                    validation_dataloader = accelerator.prepare(validation_dataloader)

                    for batch in tqdm(
                        validation_dataloader,
                        desc=f"Evaluating...",
                        position=2,
                        disable=not accelerator.is_local_main_process,
                    ):
                        # Model forward
                        eval_metric = eval_step(batch)
                        eval_metric = accelerator.gather_for_metrics(eval_metric)
                        eval_metrics.append(eval_metric)

                        # generation
                        if training_args.predict_with_generate:
                            generated_audios = generate_step(batch)
                            # Gather all predictions and targets
                            # TODO: also add prompt ids
                            # TODO: better gather
                            generated_audios, input_ids, prompts = accelerator.pad_across_processes((generated_audios, batch["input_ids"], batch["prompt_input_ids"]), dim=1, pad_index=0) 
                            generated_audios, input_ids, prompts =accelerator.gather_for_metrics((generated_audios, input_ids, prompts))
                            eval_preds.extend(generated_audios)
                            eval_descriptions.extend(input_ids)
                            eval_prompts.extend(prompts)

                    eval_time = time.time() - eval_start
                    # normalize eval metrics
                    eval_metrics = {
                        key: torch.mean(torch.stack([d[key] for d in eval_metrics])) for key in eval_metrics[0]
                    }

                    # compute metrics
                    metrics_desc = ""
                    if training_args.predict_with_generate:
                        metric_values, pred_descriptions, pred_prompts, audios, transcriptions = compute_metrics(
                            eval_preds, eval_descriptions, eval_prompts, accelerator.device
                        )
                        eval_metrics.update(metric_values)
                        metrics_desc = " ".join([f"Eval {key}: {value} |" for key, value in metric_values.items()])
                        log_pred(
                            accelerator,
                            pred_descriptions,
                            pred_prompts,
                            transcriptions,
                            audios,
                            sampling_rate=sampling_rate,
                            step=cur_step,
                            prefix="eval",
                        )

                    # Print metrics and update progress bar
                    steps_trained_progress_bar.write(
                        f"Eval results for step ({cur_step} / {total_train_steps} | Eval Loss: {eval_metrics['loss']} |"
                        f" {metrics_desc})"
                    )

                    log_metric(
                        accelerator,
                        metrics=eval_metrics,
                        train_time=eval_time,
                        step=cur_step,
                        epoch=epoch,
                        prefix="eval",
                    )

                    # flush the train metrics
                    train_start = time.time()

                # break condition
                if cur_step == total_train_steps:
                    continue_training = False
                    break

        if not continue_training:
            break

    accelerator.end_training()
    


if __name__ == "__main__":
    set_start_method("spawn")
    main()