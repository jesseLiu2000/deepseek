import torch
import json
import os
import math
import argparse
import numpy as np
import torch.nn as nn
import torch.nn.functional as F

import bitsandbytes as bnb
from bitsandbytes.nn import Linear4bit, Linear8bitLt

from transformers.activations import ACT2FN

from datasets import load_dataset
from deepspeed.pipe import PipelineModule
from transformers import AutoTokenizer, AutoModelForCausalLM, GenerationConfig
from torch.utils.data import DataLoader, Dataset, random_split
from modeling_deepseek import DeepseekMLP


parser = argparse.ArgumentParser(description="DEEPSEEK")
parser.add_argument("data", type=str, default="glue", help="input the datasets")
parser.add_argument("dataset", type=str, default="wnli", help="input the sub-dataset")
parser.add_argument(
    "subset", type=str, default="test", help="input the subset(test/val)"
)
parser.add_argument("type", type=str, default="1", help="input the connect number")
parser.add_argument("--task_idx", type=int, default="1", help="input the task index")
args = parser.parse_args()
device = torch.device("cuda:0")

# torch.cuda.set_per_process_memory_fraction(0.5, device=0)
# define model
model_name = "deepseek-ai/deepseek-moe-16b-base"
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=torch.bfloat16, device_map="auto", trust_remote_code=True)
model.generation_config = GenerationConfig.from_pretrained(model_name)
model.generation_config.pad_token_id = model.generation_config.eos_token_id
raw_state_dict = model.state_dict()
model = model.cpu() 

param_size = sum(p.nelement() * p.element_size() for p in model.parameters())
param_size_bytes = param_size / 1024 ** 2  # Convert to MB
print(f"Memory occupied by parameters: {param_size_bytes} MB")


# define dataset and input/output name
type_name = args.dataset 
dataset = load_dataset(args.data, type_name) if type_name != 'none' else load_dataset(args.data)
test_data = dataset[args.subset]

data_name = args.data 
folder_name = data_name if "/" not in data_name else data_name.split("/")[-1]
if folder_name not in ['glue', 'mmlu']:
    folder_name = "other"

output_name = type_name if type_name != "none" else args.data
output_name = output_name if "/" not in output_name else output_name.split("/")[-1]


# define quant expert (temp)
total_quant_lst = [
    [31, 56, 5, 20, 35, 0, 46, 13, 16, 14, 38, 44, 50, 62, 28, 54, 28, 40, 57, 3, 9, 17, 48, 6, 29, 48, 23],
]
quant_lst = total_quant_lst[int(args.task_idx)]

 
def flatten_2d_list(twd_list):
    return [element for od_list in twd_list for element in od_list]


# get expert info -- which experts need to duplicate
def get_expert(file_data, expert_num=8):
    layer_gap_dict = {}
    max_expert_lst = []
    layer_full_lst = [[] for idx in range(27)]

    for key in list(file_data.keys()):
        token_info = file_data[key]
        for layer_index, layer_info in enumerate(token_info):
            layer_info = flatten_2d_list(layer_info)
            layer_full_lst[layer_index].extend(layer_info)
            
    
    for layer_index, layer_info in enumerate(layer_full_lst):
        assert all(not isinstance(item, list) for item in layer_info) == True
        expert_quant = layer_info
        sample_nums = len(expert_quant)
        average_expert = sample_nums / expert_num
        expert_count_list = [expert_quant.count(i) for i in range(expert_num)]

        max_expert = np.argmax(expert_count_list)
        max_expert_tokens = expert_count_list[max_expert]
        max_expert_lst.append(max_expert)
        # print(max_expert_tokens)
        # print(average_expert)
        gap = max_expert_tokens / average_expert

        layer_gap_dict[layer_index] = gap

    return max_expert_lst, layer_gap_dict


expert_folder = f"/mnt/deepseek/eval_raw/results/raw/mmlu"
expert_data = json.load(open(os.path.join(expert_folder, f"{output_name}.json")))
max_expert_lst, layer_gap_dict = get_expert(expert_data)


# Duplicate and Quant
class DuplicateMoEGate(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.top_k = config.num_experts_per_tok
        self.n_routed_experts = config.n_routed_experts

        self.scoring_func = config.scoring_func
        self.alpha = config.aux_loss_alpha
        self.seq_aux = config.seq_aux

        # topk selection algorithm
        self.norm_topk_prob = config.norm_topk_prob
        self.gating_dim = config.hidden_size
        self.weight = nn.Parameter(torch.empty((self.n_routed_experts, self.gating_dim)))
        self.reset_parameters()

    def reset_parameters(self) -> None:
        import torch.nn.init  as init
        init.kaiming_uniform_(self.weight, a=math.sqrt(5))
    
    def forward(self, hidden_states):
        self.weight = self.weight.to(dtype=torch.bfloat16)
        bsz, seq_len, h = hidden_states.shape        
        ### compute gating score
        hidden_states = hidden_states.view(-1, h)
        logits = F.linear(hidden_states, self.weight, None)
        if self.scoring_func == 'softmax':
            scores = logits.softmax(dim=-1)
        else:
            raise NotImplementedError(f'insupportable scoring function for MoE gating: {self.scoring_func}')
        
        ### select top-k experts
        topk_weight, topk_idx = torch.topk(scores, k=self.top_k, dim=-1, sorted=False)
        print(topk_idx.shape)
        ### norm gate to sum 1
        if self.top_k > 1 and self.norm_topk_prob:
            denominator = topk_weight.sum(dim=-1, keepdim=True) + 1e-20
            topk_weight = topk_weight / denominator

        ### expert-level computation auxiliary loss
        if self.training and self.alpha > 0.0:
            scores_for_aux = scores
            aux_topk = self.top_k
            # always compute aux loss based on the naive greedy topk method
            topk_idx_for_aux_loss = topk_idx.view(bsz, -1)
            if self.seq_aux:
                scores_for_seq_aux = scores_for_aux.view(bsz, seq_len, -1)
                ce = torch.zeros(bsz, self.n_routed_experts, device=hidden_states.device)
                ce.scatter_add_(1, topk_idx_for_aux_loss, torch.ones(bsz, seq_len * aux_topk, device=hidden_states.device)).div_(seq_len * aux_topk / self.n_routed_experts)
                aux_loss = (ce * scores_for_seq_aux.mean(dim = 1)).sum(dim = 1).mean() * self.alpha
            else:
                mask_ce = F.one_hot(topk_idx_for_aux_loss.view(-1), num_classes=self.n_routed_experts)
                ce = mask_ce.float().mean(0)
                Pi = scores_for_aux.mean(0)
                fi = ce * self.n_routed_experts
                aux_loss = (Pi * fi).sum() * self.alpha
        else:
            aux_loss = None
        return topk_idx, topk_weight, aux_loss

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

print(max_expert_lst)
for idx in range(0, 27):
    # module original layers list in [1, 27] total 27 layers have gates
    quant_expert = quant_lst[idx]
    duplicate_expert = max_expert_lst[idx]
    config = model.config
    # gate_dict = model.model.layers[idx+1].mlp.gate.state_dict()
    model.model.layers[idx+1].mlp.gate = DuplicateMoEGate(config=config)
    # model.model.layers[idx+1].mlp.gate.load_state_dict(gate_dict)

    layer_dict = model.model.layers[idx+1].mlp.experts[quant_expert].state_dict()
    hidden_size = model.model.layers[idx+1].mlp.experts[quant_expert].hidden_size
    intermediate_size = model.model.layers[idx+1].mlp.experts[quant_expert].intermediate_size

    model.model.layers[idx+1].mlp.experts = nn.ModuleList([DeepseekMLP(config, intermediate_size = config.moe_intermediate_size) for i in range(config.n_routed_experts + 1)]).bfloat16()
    model.model.layers[idx+1].mlp.experts[quant_expert] = QuantDeepseekMLP(config=config, hidden_size=hidden_size, intermediate_size=intermediate_size)
    model.model.layers[idx+1].mlp.experts[64] = QuantDeepseekMLP(config=config, hidden_size=hidden_size, intermediate_size=intermediate_size)
    #print(expert_dict)
    #break


    model.model.layers[idx+1].mlp.experts[quant_expert].load_state_dict(layer_dict)
    model.model.layers[idx+1].mlp.experts[64].load_state_dict(layer_dict)
    model.model.layers[idx+1].mlp.experts[quant_expert].to(0)
    model.model.layers[idx+1].mlp.experts[64].to(0)


new_state_dict = model.state_dict()

duplicate_keyname = []
for key_idx in range(0, 27):
    duplicate_expert = max_expert_lst[key_idx]
    layer_idx = key_idx + 1
    
    duplicate_keyname.append(f"model.layers.{layer_idx}.mlp.experts.{duplicate_expert}.gate_proj.weight")
    duplicate_keyname.append(f"model.layers.{layer_idx}.mlp.experts.{duplicate_expert}.up_proj.weight")
    duplicate_keyname.append(f"model.layers.{layer_idx}.mlp.experts.{duplicate_expert}.down_proj.weight")
    duplicate_keyname.append(f"model.layers.{layer_idx}.mlp.experts.64.up_proj.weight")
    duplicate_keyname.append(f"model.layers.{layer_idx}.mlp.experts.64.down_proj.weight")
    duplicate_keyname.append(f"model.layers.{layer_idx}.mlp.experts.64.gate_proj.weight")

    # new_state_dict[new_gate_name] = raw_state_dict[old_gate_name]
    # new_state_dict[new_up_name] = raw_state_dict[old_up_name]
    # new_state_dict[new_down_name] = raw_state_dict[old_down_name]

for key in list(raw_state_dict.keys()):
    if key not in duplicate_keyname:
        new_state_dict[key] = raw_state_dict[key]
        
#for p in model.parameters():
#    print("p.nelement()", p.nelement())
#    print("p.element_size()", p.element_size())
#    p_size = (p.nelement() * p.element_size()) / 1024 ** 2
#    print(f"Memory occupied by parameters of the specific layer: {p_size} MB")

# model.load_state_dict(new_state_dict)
# param_size = sum(p.nelement() * p.element_size() for p in model.parameters())
# param_size_bytes = param_size / 1024 ** 2  # Convert to MB
# print(f"Memory occupied by parameters: {param_size_bytes} MB")

# #model.to("cpu")

# print(torch.cuda.memory_allocated())
# torch.cuda.empty_cache()
# model.to("cpu")
# model.to(device)

for param_name, param in model.named_parameters():
    if "experts" in param_name:
        print(param_name, param.dtype)


text = "An"
inputs = tokenizer(text, return_tensors="pt")
outputs = model.generate(**inputs.to(model.device), max_new_tokens=1)


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



# # with open(
# #     f"/scratch/zx22/zijie/deepseek/eval_raw/resutls/raw/mmlu/{output_name}.json", "w"
# # ) as fw:
# #     json.dump(full_expert_dict, fw, indent=4)

"""
CUDA_VISIBLE_DEVICES=7 nohup python eval_qd_model.py piqa none test 3 --task_idx 0 > ./log/raw/piqa.lb 2>&1 &
"""
