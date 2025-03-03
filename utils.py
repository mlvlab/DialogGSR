import json
import linecache
import os
import subprocess
import pickle

import torch
from torch.utils.data import DataLoader, Dataset
from torch.utils.data.distributed import DistributedSampler
from transformers import AutoTokenizer, BatchEncoding

from time import time

def is_whitespace(c):
    if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
        return True
    return False

def construct_paths(remaining_triplets, curr_path_nlp_list=[], num_hops=2):
    if len(remaining_triplets) == 0:
        if len(curr_path_nlp_list) == 0:
            return ""
        if "[TAIL]" not in curr_path_nlp_list[-1]:
            curr_path_nlp_list.append("[TAIL]")
        return "".join(curr_path_nlp_list)

    if len(curr_path_nlp_list) == 0:
        # Start new path
        return construct_paths(remaining_triplets[1:], ["[HEAD]"+remaining_triplets[0][0]+"[Int1_1][Int1_2]"+remaining_triplets[0][1].replace("_", " ").replace("-", " ")+"[Int2_1][Int2_2]"+remaining_triplets[0][2]], num_hops)
    
    last_segment = curr_path_nlp_list[-1]
    if "[Int" + str(num_hops*2) + "_1]" in last_segment or "[Rev" + str(num_hops*2) + "_1]" in last_segment:
        return construct_paths(remaining_triplets[1:], curr_path_nlp_list + ["[HEAD]"+remaining_triplets[0][0]+"[Int1_1][Int1_2]"+remaining_triplets[0][1].replace("_", " ").replace("-", " ")+"[Int2_1][Int2_2]"+remaining_triplets[0][2]], num_hops)
    else:
        for nh in range(num_hops, 0, -1):
            if "[Int"+str(nh*2)+"_2]" in last_segment:
                curr_entity = last_segment.split("[Int"+str(nh*2)+"_2]")[1].strip()
                break
            elif "[Rev"+str(nh*2)+"_2]" in last_segment:
                curr_entity = last_segment.split("[Rev"+str(nh*2)+"_2]")[1].strip()
                break
        
        if remaining_triplets[0][0] == curr_entity:
            curr_hop = nh
            if curr_hop == num_hops:
                return construct_paths(remaining_triplets[1:], curr_path_nlp_list + ["[Int"+str(curr_hop*2+1)+"_1][Int"+str(curr_hop*2+1)+"_2]"+remaining_triplets[0][1].replace("_", " ").replace("-", " ")+"[Int"+str(curr_hop*2+2)+"_1][Int"+str(curr_hop*2+2)+"_2]"+remaining_triplets[0][2]+"[TAIL]"], num_hops)
            else:
                return construct_paths(remaining_triplets[1:], curr_path_nlp_list + ["[Int"+str(curr_hop*2+1)+"_1][Int"+str(curr_hop*2+1)+"_2]"+remaining_triplets[0][1].replace("_", " ").replace("-", " ")+"[Int"+str(curr_hop*2+2)+"_1][Int"+str(curr_hop*2+2)+"_2]"+remaining_triplets[0][2]], num_hops)
        elif remaining_triplets[0][2] == curr_entity:
            curr_hop = nh
            if curr_hop == num_hops:
                return construct_paths(remaining_triplets[1:], curr_path_nlp_list + ["[Rev"+str(curr_hop*2+1)+"_1][Rev"+str(curr_hop*2+1)+"_2]"+remaining_triplets[0][1].replace("_", " ").replace("-", " ")+"[Rev"+str(curr_hop*2+2)+"_1][Rev"+str(curr_hop*2+2)+"_2]"+remaining_triplets[0][0]+"[TAIL]"], num_hops)
            else:
                return construct_paths(remaining_triplets[1:], curr_path_nlp_list + ["[Rev"+str(curr_hop*2+1)+"_1][Rev"+str(curr_hop*2+1)+"_2]"+remaining_triplets[0][1].replace("_", " ").replace("-", " ")+"[Rev"+str(curr_hop*2+2)+"_1][Rev"+str(curr_hop*2+2)+"_2]"+remaining_triplets[0][0]], num_hops)
        else:
            return construct_paths(remaining_triplets[1:], curr_path_nlp_list + ["[TAIL]"]+["[HEAD]"+remaining_triplets[0][0]+"[Int1_1][Int1_2]"+remaining_triplets[0][1].replace("_", " ").replace("-", " ")+"[Int2_1][Int2_2]"+remaining_triplets[0][2]], num_hops)

class T5Dataset(Dataset):
    def __init__(self, jsonl_file, args):
        self.args = args
        self.is_train = 'train' in jsonl_file

        self.max_length = args.max_length
        self.max_decode_step = args.max_decode_step
        self.tokenizer = args.tokenizer
        self.hist_turn = args.hist_turn
        self.file_name = jsonl_file
        self.total_size = int(subprocess.check_output(
            "wc -l " + jsonl_file, shell=True).split()[0])

        special_tokens = ['[HEAD]', '[TAIL]']
        for i in range(1, 3):
            special_tokens.extend([
                f'[Int{i*2-1}_1]', f'[Int{i*2-1}_2]',
                f'[Rev{i*2-1}_1]', f'[Rev{i*2-1}_2]',
                f'[Int{i*2}_1]', f'[Int{i*2}_2]',
                f'[Rev{i*2}_1]', f'[Rev{i*2}_2]'
            ])
        self.tokenizer.add_special_tokens({'additional_special_tokens': special_tokens})
        self.path_lim = self.args.num_paths
            
        if args.lm_type == 't5':
            self.apprentice_prefix = "apprentice: "
            self.wizard_prefix = "wizard: "
            self.knowledge_prefix = "knowledge: "
            self.prefix = "dialogue: "
            self.topic_prefix = "topic: "
        else:
            self.apprentice_prefix = ""
            self.wizard_prefix = ""
            self.knowledge_prefix = ""
            self.prefix = ""

        self.label_map = args.label_map
        with open(os.path.join(args.data_dir, "entity_codebook.pkl"), 'rb') as f:
            self.entity_codebook = pickle.load(f)
        self.reverse_entity_codebook = {v:k for k, v in self.entity_codebook.items()}

        with open(os.path.join(args.data_dir, "relation_codebook.pkl"), 'rb') as f:
            self.relation_codebook = pickle.load(f)
        self.reverse_relation_codebook = {v:k for k, v in self.relation_codebook.items()}

    
    def with_inference(self, index):
        line = linecache.getline(self.file_name, index + 1)
        json_dict = json.loads(line)
        
        bos_id = torch.tensor([self.tokenizer.pad_token_id], dtype=torch.long)
        eos_id = torch.tensor([self.tokenizer.eos_token_id], dtype=torch.long)

        dialog_history = json_dict["history"]
        prefixed_dialog_history = self.prefix + '\n '.join(dialog_history[-self.hist_turn:])

        rel_paths = json_dict["ret_triplets"]
        rel_knowledge = self.knowledge_prefix

        for idx, rel_triplets in enumerate(reversed(rel_paths)):
            if idx < 2:
                continue
            curr_rel_paths = construct_paths(rel_triplets)
            
            if len(self.tokenizer.encode(rel_knowledge+curr_rel_paths)) > self.args.knowledge_length:
                break
            else:
                rel_knowledge += curr_rel_paths


        prefixed_dialog_history =  rel_knowledge + "</s>" +  prefixed_dialog_history

        assert len(prefixed_dialog_history) > 0
        
        dialog_history_ids = self.tokenizer.encode(
            prefixed_dialog_history,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_length).squeeze(0)

        response = json_dict["label"]
        assert len(response) > 0
        
        response_ids = self.tokenizer.encode(
            response,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_decode_step).squeeze(0)
        response_ids = torch.cat([bos_id, response_ids], dim=0)

        return_data = (dialog_history_ids, response_ids)
        return return_data

    def with_train(self, index):
        line = linecache.getline(self.file_name, index + 1)
        json_dict = json.loads(line)
        
        bos_id = torch.tensor([self.tokenizer.pad_token_id], dtype=torch.long)
        eos_id = torch.tensor([self.tokenizer.eos_token_id], dtype=torch.long)

        # prefixed_dialog_history = []
        dialog_history = json_dict["history"]
        # prefixed_dialog_history = self.prefix + ' '.join(dialog_history)
        prefixed_dialog_history = self.prefix + '\n '.join(dialog_history[-self.hist_turn:])
        # checked_knowledge = self.knowledge_prefix + json_dict['checked_knowledge']

        tot_knowledge = self.knowledge_prefix

        rel_paths = json_dict["ret_triplets"]

        for idx, rel_triplets in enumerate(reversed(rel_paths)):
            if idx < 2:
                continue
            curr_rel_paths = construct_paths(rel_triplets)
            
            if len(self.tokenizer.encode(tot_knowledge+curr_rel_paths)) > self.args.knowledge_length:
                break
            else:
                tot_knowledge += curr_rel_paths


        prefixed_dialog_history =  tot_knowledge + "</s>" +  prefixed_dialog_history

        assert len(prefixed_dialog_history) > 0
        dialog_history_ids = self.tokenizer.encode(
            prefixed_dialog_history,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_length).squeeze(0)

        response = json_dict["label"]
        assert len(response) > 0
        
        # Tokenize response
        response_ids = self.tokenizer.encode(
            response,
            return_tensors="pt",
            truncation=True,
            max_length=self.max_decode_step).squeeze(0)
        response_ids = torch.cat([bos_id, response_ids], dim=0)
        

        return_data = (dialog_history_ids, response_ids)
        return return_data

    def __getitem__(self, index):
        if self.is_train:
            return self.with_train(index)
        else:
            return self.with_inference(index)

    def __len__(self):
        return self.total_size

class Dialprocessor(object):
    def __init__(self, args):
        self.train_file = "train.jsonl"
        self.dev_file = "valid.jsonl"
        self.test_file = "test.jsonl"

        self.args = args
        args.dev_file = self.dev_file
        args.test_file = self.test_file

    def get_train_examples(self, data_dir):
        print(f"DataProcessor: {self.train_file}")
        return T5Dataset(os.path.join(data_dir, self.train_file), args=self.args)

    def get_dev_examples(self, data_dir):
        print(f"DataProcessor: {self.dev_file}")
        return T5Dataset(os.path.join(data_dir, self.dev_file), args=self.args)

    def get_test_examples(self, data_dir):
        print(f"DataProcessor: {self.test_file}")
        return T5Dataset(os.path.join(data_dir, self.test_file), args=self.args)

def load_raw_dataset(args, fold):
    if fold == "train":
        filename = "train.jsonl"
    elif fold == "dev":
        filename = "valid.jsonl"
    else:
        filename = "test.jsonl"

    datafile = os.path.join(args.data_dir, filename)
    with open(datafile, 'r') as f:
        dataset = [json.loads(data) for data in f.readlines()]
    return dataset

class Profiler(object):
    def __init__(self, args):
        with open(os.path.join(args.data_dir, "entity_codebook.pkl"), 'rb') as f:
            self.entity_codebook = pickle.load(f)
        self.reverse_entity_codebook = {v:k for k, v in self.entity_codebook.items()}

        with open(os.path.join(args.data_dir, "relation_codebook.pkl"), 'rb') as f:
            self.relation_codebook = pickle.load(f)
        self.reverse_relation_codebook = {v:k for k, v in self.relation_codebook.items()}
        self.tokenizer = args.tokenizer
        self.reverse_label_map = {v:k for k, v in args.label_map.items()}

    def write_profile(self,
                      profile_fw,
                      data,
                      new_input_ids,
                      pred_response_token,
                      path_ids,
                      batch_idx):
        headline = f"Episode {data['episode_id']}, Turn {data['turn_id']}"
        history = "HISTORY ==================\n" + '\n'.join(data['history'])
        response = "GT RESPONSE ================\n" + data["label"]
        preds = "PREDICTIONS =================\n" + pred_response_token.strip()
        # entities = data["history_entities"]
        # knowledges = "ENTITIES ====================\n" + ', '.join(entities)
        # knowledges += f"# Knowledge entities: {len(entities)}\n"
        gold_knowledges = ""
        for gt in data["gold_triplets"]:
            gold_knowledges += " ".join(gt)
            gold_knowledges += "\n"
            
        gold_knowledges = "GOLD_knowledges ====================\n" + gold_knowledges
        new_history = self.tokenizer.decode(new_input_ids.cpu(),
                                skip_special_tokens=True,
                                clean_up_tokenization_spaces=False)
        new_history = ("Selected FACT + HISTORY ============\n" + new_history).strip()

        profile_fw.write(headline + '\n')
        profile_fw.write(history + '\n')
        profile_fw.write(response + '\n')
        profile_fw.write(new_history + '\n')
        profile_fw.write(gold_knowledges)
        profile_fw.write(preds + '\n\n\n')

            
        profile_fw.flush()
            

