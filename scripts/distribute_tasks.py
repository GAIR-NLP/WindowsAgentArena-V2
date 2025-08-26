import os
import json
import argparse
import math

task_list = []
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)


def distribute_task(concurrent_num, result_dir):
    """
    Distribute tasks to concurrent instances
    Return: True if no need to distribute tasks, False otherwise
    """
    test_custom_path = os.path.join(root_dir, 'src/win-arena-container/client/evaluation_examples_windows/test_custom.json')
    
    # read custom task
    try:
        with open(test_custom_path, 'r', encoding='utf-8') as f:
            all_task_data = json.load(f)
            if not all_task_data:
                return True
    except Exception as e:
        return True
    
    for category, tasks in all_task_data.items():
        for task in tasks:
            task_dir = os.path.join(result_dir, category, task)
            # check if the task directory exists
            if not os.path.exists(task_dir):
                task_list.append({"category": category, "task_id": task})

    # calculate the number of tasks per file
    tasks_per_file = math.ceil(len(task_list) / concurrent_num)
    
    # ensure the target directory exists
    concurrent_eval_dir = os.path.join(root_dir, 'src/win-arena-container/client/evaluation_examples_windows/concurrent_eval')
    os.makedirs(concurrent_eval_dir, exist_ok=True)

    # clear all files in the target directory
    for item in os.listdir(concurrent_eval_dir):
        item_path = os.path.join(concurrent_eval_dir, item)
        if os.path.isfile(item_path) or os.path.islink(item_path):
            os.unlink(item_path)
    
    # distribute tasks and write to files
    for i in range(concurrent_num):
        distributed_task_custom_path = os.path.join(root_dir, f'src/win-arena-container/client/evaluation_examples_windows/concurrent_eval/test_custom_{i+1}.json')
        distributed_task_current_path = os.path.join(root_dir, f'src/win-arena-container/client/evaluation_examples_windows/concurrent_eval/test_current_{i+1}.json')
        
        # calculate the index range of tasks in the current file
        start_idx = i * tasks_per_file
        end_idx = min((i + 1) * tasks_per_file, len(task_list))
        
        # extract tasks from the current file
        current_tasks = task_list[start_idx:end_idx]
        
        # organize data in the original format
        distributed_data = {}
        for task_item in current_tasks:
            category = task_item["category"]
            task_id = task_item["task_id"]
            
            if category not in distributed_data:
                distributed_data[category] = []
            
            distributed_data[category].append(task_id)
        
        # write to files
        with open(distributed_task_custom_path, 'w', encoding='utf-8') as f:
            json.dump(distributed_data, f, ensure_ascii=False, indent=4)
        
        # clear the current file
        open(distributed_task_current_path, 'w').close()
            
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrent_num", type=int, default=4)
    parser.add_argument('--trial_id', type=str, required=True, help='The trial ID for this run')
    parser.add_argument('--agent', type=str, required=True, help='The agent name')
    args = parser.parse_args()

    # build result dir
    result_dir = os.path.join(root_dir, "src/win-arena-container/client/results", args.trial_id, args.agent)

    no_need_to_distribute = distribute_task(args.concurrent_num, result_dir)
    if no_need_to_distribute:
        print(1)
    else:
        print(0)
