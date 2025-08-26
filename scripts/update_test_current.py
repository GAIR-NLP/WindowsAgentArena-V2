import os
import json
import argparse

task_list = []
current_task_list = [] # this list only contains one task, which is the current task

current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)

def update_current_task(result_dir, concurrent_idx):
    """
    Update the current task to the test_current.json file or text_current_concurrent_idx.json file
    Return: True if all tasks are completed, False otherwise
    """
    test_custom_path = os.path.join(root_dir, f'src/win-arena-container/client/evaluation_examples_windows/concurrent_eval/test_custom_{concurrent_idx}.json')
    test_current_path = os.path.join(root_dir, f'src/win-arena-container/client/evaluation_examples_windows/concurrent_eval/test_current_{concurrent_idx}.json')
    
    # Read custom task
    with open(test_custom_path, 'r', encoding='utf-8') as f:
        all_task_data = json.load(f)

    for category, tasks in all_task_data.items():
        for task in tasks:
            task_list.append({"category": category, "task_id": task})

    # If there is no task in the test_custom.json, return True
    if len(task_list) == 0:
        return True

    # If the test_current.json file exists, read it
    if os.path.exists(test_current_path): 
        try:
            with open(test_current_path, 'r', encoding='utf-8') as f:
                current_task_data = json.load(f)
            
            # check if the data is empty
            if not current_task_data:
                next_task_id = 0
            else:
                for category, tasks in current_task_data.items():
                    for task in tasks:
                        current_task_list.append({"category": category, "task_id": task})
                assert len(current_task_list) == 1, "There should be only one task in the current_task_list"

                # the the id of the current task id in entire task list
                current_task_id = -1
                for i, task in enumerate(task_list):
                    if task["category"] == current_task_list[0]["category"] and task["task_id"] == current_task_list[0]["task_id"]:
                        current_task_id = i
                        break
                assert current_task_id != -1, "The current task is not in the task list"
                
                if current_task_id == len(task_list) - 1: # all tasks have been evaluated
                    return True
                
                next_task_id = current_task_id + 1 # the next task id
        except (json.JSONDecodeError, ValueError):
            # the file is empty or the format is wrong, start from the beginning
            next_task_id = 0
    else:
        next_task_id = 0
  
    # Get the next task
    next_task = task_list[next_task_id]
    next_task_dir = os.path.join(result_dir, next_task["category"], next_task["task_id"])

    # If the next task has already been processed, skip it
    while os.path.exists(next_task_dir):
        next_task_id += 1
        if next_task_id == len(task_list):
            return True
        next_task = task_list[next_task_id]
        next_task_dir = os.path.join(result_dir, next_task["category"], next_task["task_id"])
    
    with open(test_current_path, 'w', encoding='utf-8') as f:
        json.dump({next_task["category"]: [next_task["task_id"]]}, f, ensure_ascii=False, indent=4)
    return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process tasks with a trial ID')
    parser.add_argument('--trial_id', type=str, required=True, help='The trial ID for this run')
    parser.add_argument('--agent', type=str, required=True, help='The agent name')
    parser.add_argument('--concurrent_idx', type=int, default=0, help='The index of the concurrent task')

    # Parse arguments
    args = parser.parse_args()
    trail_id = args.trial_id
    agent = args.agent
    concurrent_idx = args.concurrent_idx

    result_dir = os.path.join(root_dir, 'src/win-arena-container/client/results', trail_id, agent)
    
    result = update_current_task(result_dir, concurrent_idx)
    if result:
        print(1)  # All tasks have been evaluated
    else:
        print(0) # There are still tasks to be evaluated