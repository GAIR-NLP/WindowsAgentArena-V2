#!/bin/bash

LOG_BASE_DIR="../logs"

# Revert VM snapshot to base state
revert_vm_snapshot() {
    local concurrent_idx=$1
    local log_file=$2

    backup_idx=$(((concurrent_idx - 1) % 4 + 1 ))

    vm_storage_mount_path="../src/win-arena-container/vm/storage_${concurrent_idx}"
    vm_storage_backup_mount_path="../src/win-arena-container/vm/storage_backup_${backup_idx}"

    # Create storage directory if it doesn't exist
    if [ ! -d "$vm_storage_mount_path" ]; then
        mkdir -p "$vm_storage_mount_path"
    fi

    # Clear storage directory, which contains the modified Windows system checkpoint from the last run
    start_time=$(date +%s)
    echo "Reverting $vm_storage_mount_path..." >> "$log_file"
    if [[ -d "$vm_storage_mount_path" ]]; then
        rm -rf "$vm_storage_mount_path"/*
        clear_time=$(date +%s)
    else
        clear_time=$(date +%s)
    fi

    # Copy initial Windows system checkpoint from backup directory to storage directory
    echo "Begin to copy $vm_storage_backup_mount_path..." >> "$log_file"
    if [[ -d "$vm_storage_backup_mount_path" ]]; then
        cp -r "$vm_storage_backup_mount_path"/* "$vm_storage_mount_path"
        copy_time=$(date +%s)
    else
        copy_time=$(date +%s)
    fi

    end_time=$(date +%s)
    echo "Revert completed in $((end_time - start_time)) seconds" >> "$log_file"
}

remove_vm_snapshot(){
    local concurrent_idx=$1
    local log_file=$2
    vm_storage_mount_path="../src/win-arena-container/vm/storage_${concurrent_idx}"

    # Remove storage directory
    start_time=$(date +%s)
    echo "Removing $vm_storage_mount_path..." >> "$log_file"
    if [[ -d "$vm_storage_mount_path" ]]; then
        rm -rf "$vm_storage_mount_path"
        clear_time=$(date +%s)
    else
        clear_time=$(date +%s)
    fi

    echo "Remove completed in $((clear_time - start_time)) seconds" >> "$log_file"
}

# Function to start a single instance
run_instance() {
    local concurrent_idx=$1
    local model=$2
    local trial_id=$3
    local agent=$4
    local max_steps=$5
    local check_setup=$6
    local run_count=$7

    # Create dedicated log directory for current trial_id and instance
    local instance_log_dir="$LOG_BASE_DIR/trial_${trial_id}/instance_${concurrent_idx}"
    if [ ! -d "$instance_log_dir" ]; then
        mkdir -p "$instance_log_dir"
    fi

    local log_file="$instance_log_dir/run${run_count}.log"

    # Initialize log file
    echo "=== Instance $concurrent_idx Run $run_count Log Started at $(date) ===" > "$log_file"
    echo "Starting instance with concurrent_idx=$concurrent_idx, model=$model, trial_id=$trial_id, run=$run_count" >> "$log_file"

    while true; do
        echo "Restart the container and run the next task for instance $concurrent_idx..." >> "$log_file"

        # Update current task
        python_output=$(python3 "./update_test_current.py" --agent $agent --trial_id $trial_id --concurrent_idx $concurrent_idx)
        if [ "$python_output" -eq 1 ]; then
            echo "All tasks for instance $concurrent_idx are completed! There is no next task to run."
            exit 0
        fi

        # Revert VM snapshot
        revert_vm_snapshot "$concurrent_idx" "$log_file"

        ./run-local.sh --agent "$agent" --model "$model" --trial-id "$trial_id" --skip-build true --max-steps $max_steps --concurrent true --concurrent-idx $concurrent_idx --check-setup $check_setup >> "$log_file" 2>&1

    done

    test_current_path="../src/win-arena-container/client/evaluation_examples_windows/concurrent_eval/test_current_${concurrent_idx}.json"
    rm "$test_current_path"

    echo "Instance $concurrent_idx has completed its tasks for run $run_count." >> "$log_file"
    remove_vm_snapshot "$concurrent_idx" "$log_file"

    docker kill winarena-v2_${concurrent_idx} >> "$log_file" 2>&1

    echo "=== Instance $concurrent_idx Run $run_count Log Completed at $(date) ===" >> "$log_file"
}

source /etc/profile.d/clash.sh
proxy_on

# Build container image
./build-container-image.sh

# Load parameters from config.json
CONFIG_FILE="../config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Config file not found: $CONFIG_FILE"
    exit 1
fi
model=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("model",""))' "$CONFIG_FILE")
initial_trial_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("trial_id",""))' "$CONFIG_FILE")
max_steps=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("max_steps",""))' "$CONFIG_FILE")
agent=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("agent",""))' "$CONFIG_FILE")
CONCURRENT_NUM=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("CONCURRENT_NUM",""))' "$CONFIG_FILE")

if [ -z "$model" ] || [ -z "$initial_trial_id" ] || [ -z "$max_steps" ] || [ -z "$agent" ] || [ -z "$CONCURRENT_NUM" ]; then
    echo "Missing required config fields in $CONFIG_FILE: model, trial_id, max_steps, agent, CONCURRENT_NUM"
    exit 1
fi

# Calculate end trial ID
end_trial_id=$((initial_trial_id + 9))

echo "Starting multi-trial experiment from trial $initial_trial_id to trial $end_trial_id"

# Main trial loop
for ((current_trial_id=initial_trial_id; current_trial_id<end_trial_id; current_trial_id++)); do
    echo "=========================================="
    echo "Starting Trial $current_trial_id (of $end_trial_id)"
    echo "=========================================="

    # Create dedicated log directory for current trial_id
    LOG_DIR="$LOG_BASE_DIR/trial_${current_trial_id}"
    if [ ! -d "$LOG_DIR" ]; then
        mkdir -p "$LOG_DIR"
    fi

    # Create and initialize main log file
    main_log="$LOG_DIR/main.log"
    echo "=== Trial $current_trial_id Main Process Started at $(date) ===" > "$main_log"
    echo "=== Trial $current_trial_id: $model, $agent, max_steps=$max_steps ===" >> "$main_log"

    # Run 3 times of task distribution and execution for current trial
    for run_count in {1..3}; do
        echo "=== Starting Trial $current_trial_id - Run $run_count of 3 ==="

        # Set check_setup parameter: need to check environment setup correctness in first and second runs, no need in third run
        if [ $run_count -eq 3 ]; then
            check_setup_flag="false"
        else
            # check_setup_flag="true"
            check_setup_flag="false"
        fi

        # Distribute tasks from test_custom to each instance
        echo "Distributing tasks to $CONCURRENT_NUM instances (Trial $current_trial_id, run $run_count)..."
        python_output=$(python3 "./distribute_tasks.py" --concurrent_num $CONCURRENT_NUM --trial_id $current_trial_id --agent $agent)
        if [ "$python_output" -eq 1 ]; then
            echo "All tasks are completed for Trial $current_trial_id! Skipping to next trial."
            break 2  # Break out of both inner and outer loops
        fi

        # Write current run start information
        echo "== Trial $current_trial_id - Run $run_count Started with $CONCURRENT_NUM concurrent instances at $(date) ==" >> "$main_log"
        echo "== Trial $current_trial_id - Run $run_count Started with $CONCURRENT_NUM concurrent instances at $(date) =="

        # Collect PIDs of all instances
        instance_pids=()

        for ((i=1; i<=CONCURRENT_NUM; i++)); do
            run_instance "$i" "$model" "$current_trial_id" "$agent" "$max_steps" "$check_setup_flag" "$run_count"&
            pid=$!
            instance_pids+=($pid)
            echo "Started instance $i with PID $pid (Trial $current_trial_id, run $run_count)" | tee -a "$main_log"
            # Add small delay to prevent resource contention
            sleep 5
        done

        # Display command to terminate all instances at once
        echo "To kill all instances at once, cd to scripts directory and run: sudo ./kill_container.sh ${instance_pids[*]}" | tee -a "$main_log"
        echo "Note: You may need to quit the main process or exit screen (screen -S winarena -X quit) before killing containers." | tee -a "$main_log"

        # Wait for all background processes to complete
        wait
        echo "== Trial $current_trial_id - Run $run_count Completed at $(date) ==" >> "$main_log"
        echo "== Trial $current_trial_id - Run $run_count Completed at $(date) =="
        echo ""
    done

    echo "=== Trial $current_trial_id completed successfully! ===" >> "$main_log"
    echo "Trial $current_trial_id completed successfully!"
    echo ""

    # Add a small delay between trials to allow system cleanup
    sleep 10
done

echo "All trials from $initial_trial_id to $end_trial_id completed successfully!"
