import datetime
import os
import json
import html as html_lib
import numpy as np
from typing import Dict, Any, List, Tuple

class TrajectoryRecorder:
    def __init__(self, result_dir: str):
        self.result_dir = result_dir
        
    def save_dict(self, info_dict: Dict[str, Any], step_idx: int, action_timestamp: str) -> dict:
        """
        Save each key of the observation to the specified path, parsing the correct datatypes.
        """
        file_format = "{key}-step_{step_idx}_{action_timestamp}.{ext}"
        obs_content = {k:None for k in info_dict.keys()}
        
        for key, value in info_dict.items():
            if value is None:
                obs_content.pop(key)
                continue
            file_path = None
            if key in ["accessibility_tree", "user_question"]:  # "plan_results"
                file_path = os.path.join(self.result_dir, file_format.format(
                    key=key, step_idx=step_idx, action_timestamp=action_timestamp, ext="txt"))
                with open(file_path, "w") as f:
                    f.write(value if value else "No data available")
                obs_content[key] = os.path.basename(file_path)
                
            elif isinstance(value, bytes):
                os.makedirs(os.path.join(self.result_dir, key), exist_ok=True)
                file_path = os.path.join(self.result_dir, key, file_format.format(
                    key=key, step_idx=step_idx, action_timestamp=action_timestamp, ext="png"))
                with open(file_path, "wb") as f:
                    f.write(value)
                obs_content[key] = os.path.join(key, os.path.basename(file_path))
                
            elif isinstance(value, (int, float)):
                obs_content[key] = value
                
            elif isinstance(value, (list, tuple, np.ndarray)) and len(value) > 0 and isinstance(value[0], (int, float)):
                obs_content[key] = value
                
            elif isinstance(value, str):
                obs_content[key] = value
                
            elif isinstance(value, np.ndarray):
                file_path = os.path.join(self.result_dir, file_format.format(
                    key=key, step_idx=step_idx, action_timestamp=action_timestamp, ext="npy"))
                np.save(file_path, value)
                obs_content[key] = os.path.join(key, os.path.basename(file_path))
                
            elif "PIL" in str(type(value)):
                os.makedirs(os.path.join(self.result_dir, key), exist_ok=True)
                file_path = os.path.join(self.result_dir, key, file_format.format(
                    key=key, step_idx=step_idx, action_timestamp=action_timestamp, ext="png"))
                value.save(file_path)
                obs_content[key] = os.path.join(key, os.path.basename(file_path))
                
            elif isinstance(value, (dict, list)):
                file_path = os.path.join(self.result_dir, file_format.format(
                    key=key, step_idx=step_idx, action_timestamp=action_timestamp, ext="json"))
                with open(file_path, "w") as f:
                    json.dump(value, f)
                obs_content[key] = os.path.basename(file_path)
                
            else:
                obs_content[key] = f"key: {key}: {type(value)} not saved"
                
        return obs_content

    def record_init(self, obs: Dict[str, Any], example: Dict[str, Any], init_timestamp: str) -> None:
        """Record initial state"""
        init_dict = self.save_dict(obs, 'reset', init_timestamp)
        
        # Save to JSONL
        with open(os.path.join(self.result_dir, "traj.jsonl"), "a") as f:
            traj_data = {
                "step_num": 0,
                "action_timestamp": init_timestamp,
                "action": None
            }
            traj_data.update(init_dict)
            json.dump(traj_data, f)
            f.write("\n")
        

    def record_step(self, obs: Dict[str, Any], logs: Dict[str, Any], 
                   step_idx: int, action_timestamp: str,action: str) -> None:
        """Record a single step"""
        obs_saved_content = self.save_dict(obs, step_idx, action_timestamp) if obs else {}
        logs_saved_content = self.save_dict(logs, step_idx, action_timestamp) if logs else {}
        
        # Save to JSONL
        with open(os.path.join(self.result_dir, "traj.jsonl"), "a") as f:
            traj_data = {
                "step_num": step_idx + 1,
                "action_timestamp": action_timestamp,
                "action": action,
            }
            traj_data.update(obs_saved_content)
            traj_data.update(logs_saved_content)
            json.dump(traj_data, f)
            f.write("\n")
        
        # Save to Markdown
        with open(os.path.join(self.result_dir, "traj.md"), "a") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                instruction = obs.get('instruction', 'No instruction provided')
                f.write(f"### {instruction}\n\n")
                
            md = []
            md.append(f"### Step {step_idx + 1} \n")
            formatted_timestamp = datetime.datetime.strptime(action_timestamp, "%Y%m%d@%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            md.append(f"#### {formatted_timestamp}\n")
            md.append(f"#### Observation\n")
            if obs_image := obs_saved_content.get('screenshot'):
                md.append(f"![Screenshot]({obs_image})\n")
            else:
                raise ValueError("No image!")
            md.append(f"#### Plan\n")
            md.append(f"{logs.get('plan_result')}\n")
            md.append(f"#### Action\n")
            action = '' if action is None else action
            md.append(f"```\n{action}\n```\n")
            f.write("".join(md))

    def record_end(self, scores: List[Tuple[int, float]], obs: Dict[str, Any], step_idx: int, action_timestamp: str) -> None:
        """Record evaluation results"""

        # Record final observation
        obs_saved_content = self.save_dict(obs, step_idx, action_timestamp) if obs else {}

        # Record final state in traj.md
        with open(os.path.join(self.result_dir, "traj.md"), "a") as f:
            for idx, result in enumerate(scores):
                final_step_num, final_score = result
                f.write(f"### Score at step {final_step_num}: {final_score}\n")
            
            f.write(f"### Final Observation\n")
            if obs_image := obs_saved_content.get('screenshot'):
                f.write(f"![Screenshot]({obs_image})\n")
            else:
                raise ValueError("No image!")

        # Record final score in result.txt
        with open(os.path.join(self.result_dir, "result.txt"), "w", encoding="utf-8") as f:
            final_step_num, final_score = scores[-1]
            f.write(f"{final_score}\n")

        # Record all scores in results.json
        with open(os.path.join(self.result_dir, "results.json"), "w", encoding="utf-8") as f:
            results = [{"step": step_num, "score": score} for step_num, score in scores]
            json.dump(results, f, indent=4)
        
    