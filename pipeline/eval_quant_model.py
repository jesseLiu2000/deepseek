import torch
import json
import argparse
import numpy as np
import torch.nn as nn
import torch.nn.functional as F

import bitsandbytes as bnb
from bitsandbytes.nn import Linear4bit

from datasets import load_dataset
from deepspeed.pipe import PipelineModule
from transformers.activations import ACT2FN
from transformers import AutoTokenizer, AutoModelForCausalLM, GenerationConfig
from torch.utils.data import DataLoader, Dataset, random_split
from configuration_deepseek import DeepseekConfig


parser = argparse.ArgumentParser(description="LLAMAMOE")
parser.add_argument("data", type=str, default="glue", help="input the datasets")
parser.add_argument("dataset", type=str, default="wnli", help="input the sub-dataset")
parser.add_argument(
    "subset", type=str, default="test", help="input the subset(test/val)"
)
parser.add_argument("type", type=str, default="1", help="input the connect number")
parser.add_argument(
    "--sub_one", type=str, default="question", help="input the sub-type"
)
parser.add_argument(
    "--sub_two", type=str, default="sentence", help="input the sub-type"
)
parser.add_argument(
    "--sub_three", type=str, default="sentence", help="input the sub-type"
)
args = parser.parse_args()
device = torch.device("cuda")


model_name = "deepseek-ai/deepseek-moe-16b-base"
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=torch.bfloat16, device_map="auto", trust_remote_code=True)
model.generation_config = GenerationConfig.from_pretrained(model_name)
model.generation_config.pad_token_id = model.generation_config.eos_token_id
model_dict  = model.state_dict()
# model.to(device)
# model.eval()

class QuantDeepseekMLP(nn.Module):
    def __init__(self, config, hidden_size = None, intermediate_size = None):
        super().__init__()
        self.config = config
        self.hidden_size = config.hidden_size if hidden_size is None else hidden_size
        self.intermediate_size = config.intermediate_size if intermediate_size is None else intermediate_size

        self.gate_proj = Linear4bit(self.hidden_size, self.intermediate_size, bias=False)
        self.up_proj = Linear4bit(self.hidden_size, self.intermediate_size, bias=False)
        self.down_proj = Linear4bit(self.intermediate_size, self.hidden_size, bias=False)

        self.act_fn = ACT2FN[config.hidden_act]

    def forward(self, x):
        if self.config.pretraining_tp > 1:
            slice = self.intermediate_size // self.config.pretraining_tp
            gate_proj_slices = self.gate_proj.weight.split(slice, dim=0)
            up_proj_slices = self.up_proj.weight.split(slice, dim=0)
            down_proj_slices = self.down_proj.weight.split(slice, dim=1)

            gate_proj = torch.cat(
                [F.linear(x, gate_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1
            )
            up_proj = torch.cat([F.linear(x, up_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1)

            intermediate_states = (self.act_fn(gate_proj) * up_proj).split(slice, dim=2)
            down_proj = [
                F.linear(intermediate_states[i], down_proj_slices[i]) for i in range(self.config.pretraining_tp)
            ]
            down_proj = sum(down_proj)
        else:
            down_proj = self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))

        return down_proj

for layer in model.model.layers[1: 27]:
    config = DeepseekConfig()
    hidden_size = layer.mlp.experts[0].hidden_size
    intermediate_size = layer.mlp.experts[0].intermediate_size
    layer.mlp.experts[0] = QuantDeepseekMLP(config=config, hidden_size=hidden_size, intermediate_size=intermediate_size)

print(model)

# class GLUEDataset(Dataset):
#     def __init__(self, sequences):
#         self.sequences = sequences

#     def __len__(self):
#         return len(self.sequences)

#     def __getitem__(self, idx):
#         sequences = self.sequences[idx]
#         return sequences


# def collate_fn(batch):
#     """Define the collate function for dataloader"""
#     sequences = batch
#     inputs = tokenizer(sequences, return_tensors="pt")
#     return inputs


# def get_layer_output(module, input, output):
#     expert_idx = output[0].detach().cpu().tolist()
#     layer_outputs.append(expert_idx)


# type_name = args.dataset 
# dataset = load_dataset(args.data, type_name) if type_name != 'none' else load_dataset(args.data)
# test_data = dataset[args.subset]

# if args.type == "2":
#     full_sentence = []
#     for q, s in zip(test_data[args.sub_one], test_data[args.sub_two]):
#         prompt = q + s
#         full_sentence.append(prompt)

# elif args.type == "1":
#     full_sentence = test_data[args.sub_one]
    
# elif args.type == "3":
#     full_sentence = []
#     for q, s, z in zip(
#         test_data[args.sub_one], test_data[args.sub_two], test_data[args.sub_three]
#     ):
#         # prompt = q + " ".join(s["choices"]) + " ".join(z["choices"])
#         prompt = q + s + z
#         full_sentence.append(prompt)


# ax_dataset = GLUEDataset(full_sentence)
# ax_dataloader = DataLoader(
#     ax_dataset, batch_size=1, shuffle=False, collate_fn=collate_fn
# )

# full_expert_dict = {}
# with torch.no_grad():
#     for idx, inputs in enumerate(ax_dataloader):
#         inputs = inputs.to(device)
#         layer_outputs = []
#         hooks = []
#         for decoder_layer in model.model.layers[1:-1]:
#             hook = decoder_layer.mlp.gate.register_forward_hook(
#                 get_layer_output
#             )
#             hooks.append(hook)

#         result = model.generate(**inputs.to(model.device), max_new_tokens=1)
#         full_expert_dict[idx] = layer_outputs

#         for hook in hooks:
#             hook.remove()


# output_name = type_name if type_name != "none" else args.data
# output_name = output_name if "/" not in output_name else output_name.split("/")[-1]

# with open(
#     f"/scratch/zx22/zijie/deepseek/eval_raw/resutls/raw/mmlu/{output_name}.json", "w"
# ) as fw:
#     json.dump(full_expert_dict, fw, indent=4)



"""
CUDA_VISIBLE_DEVICES=0 nohup python eval_quant_model.py piqa none test 3 --sub_one goal --sub_two sol1 --sub_three sol2 > ./log/raw/piqa.lb 2>&1 &
CUDA_VISIBLE_DEVICES=5 nohup python eval_quant_model.py winogrande winogrande_debiased test 3 --sub_one sentence --sub_two option1 --sub_three option2 > ./log/raw/winogrande_debiased.lb 2>&1 &
CUDA_VISIBLE_DEVICES=6 nohup python eval_quant_model.py Rowan/hellaswag none test 3 --sub_one ctx_a --sub_two ctx_b --sub_three activity_label > ./log/raw/hellaswag.lb 2>&1 &
CUDA_VISIBLE_DEVICES=7 nohup python eval_quant_model.py truthful_qa generation validation 2 --sub_one question --sub_two best_answer > ./log/raw/generation.lb 2>&1 &
CUDA_VISIBLE_DEVICES=1 nohup python eval_quant_model.py truthful_qa multiple_choice validation 3 --sub_one question --sub_two mc1_targets --sub_three mc2_targets > ./log/raw/multiple_choice.lb 2>&1 &
CUDA_VISIBLE_DEVICES=6 nohup python eval_quant_model.py gsm8k main test 2 --sub_one question --sub_two answer > ./log/raw/main.lb 2>&1 &
CUDA_VISIBLE_DEVICES=7 nohup python eval_quant_model.py gsm8k socratic test 2 --sub_one question --sub_two answer > ./log/raw/socratic.lb 2>&1 &


"""
