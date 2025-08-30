import os
import json


def count_files_and_calculate_average(result_dir, isroot=False):
    
    traj_jsonl_count = 0

    result_txt_count = 0
    results_json_count = 0
    error_txt_paths = []
    total_score = 0
    result_txt_files = 0

    for root, dirs, files in os.walk(result_dir):
        for file in files:

            file_path = os.path.join(root, file)
            task_id = os.path.basename(os.path.dirname(file_path))

            if file == 'traj.jsonl':
                traj_jsonl_count += 1
            elif file == 'results.json':
                results_json_count += 1
                with open(file_path, "r", encoding="utf-8") as file:
                    data = json.load(file)

                scores = [entry["score"] for entry in data]
                total_score += scores[-1] 
    
            elif file == 'error.txt':
                error_txt_paths.append(os.path.join(root, file))

    if traj_jsonl_count != 0:
        average_score = total_score / traj_jsonl_count * 100
    else:
        average_score = 0

    print(f"Number of traj.jsonl files: {traj_jsonl_count}")
    print(f"Number of results.json files: {results_json_count}")
    print(f"Number of error.txt files: {len(error_txt_paths)}")

    print(f"Error.txt file paths:")
    for path in error_txt_paths:
        print(path)
    print(f"Average score in results.json files: {average_score}\n")

result_dir = "src/win-arena-container/client/results/1/pcagent" # replace with your result directory

count_files_and_calculate_average(result_dir, True)

for subdir in os.listdir(result_dir):
    # Name of the last level directory
    subdir_name = os.path.basename(subdir)
    print(f"------- Category: {subdir_name} -------")
    subdir_path = os.path.join(result_dir, subdir)
    if os.path.isdir(subdir_path):
        count_files_and_calculate_average(subdir_path, False)
