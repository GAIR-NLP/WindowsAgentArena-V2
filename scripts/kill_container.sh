#!/bin/bash

# Check if arguments are provided
if [ $# -eq 0 ]; then
    echo "Usage: $0 <PID1> <PID2> ... <PIDN>"
    echo "Example: $0 1953417 1953481 1953547 1953615"
    exit 1
fi

# Get the number of process IDs provided
CONCURRENT_NUM=$#

echo "About to terminate $CONCURRENT_NUM processes and corresponding containers..."

# Kill all provided process IDs
echo "Terminating processes... If a PID does not exist, it will be skipped."
failed_pids=()
for pid in "$@"
do
    if kill -0 $pid 2>/dev/null; then # Check if process exists
        echo "Killing process: $pid"
        if sudo kill $pid 2>/dev/null; then
            # Check if process is actually terminated
            sleep 1
            if kill -0 $pid 2>/dev/null; then
                echo "Warning: Process $pid may still be running"
                failed_pids+=($pid)
            else
                echo "Process $pid terminated successfully"
            fi
        else
            echo "Warning: Failed to kill process $pid (permission denied or other error)"
            failed_pids+=($pid)
        fi
    else
        echo "Process $pid does not exist (already terminated or never started), skipping."
    fi
done

echo "Removing Docker containers..."
failed_containers=()
# remove container
for id in $(seq 1 $CONCURRENT_NUM)
do 
    container_name="winarena-v2_${id}"
    echo "Removing container: $container_name"
    
    # Check if container exists first
    if sudo docker ps -a --format "{{.Names}}" | grep -q "^${container_name}$"; then
        # First attempt to stop and remove the container
        if sudo docker rm -f $container_name 2>/dev/null; then
            # Double check if container is really gone
            sleep 1
            if sudo docker ps -a --format "{{.Names}}" | grep -q "^${container_name}$"; then
                echo "Warning: Container $container_name may still exist after first attempt"
                failed_containers+=($container_name)
            else
                echo "Container $container_name stopped and removed successfully"
            fi
        else
            echo "First attempt failed, trying again to remove container: $container_name"
            # Second attempt after a brief wait
            sleep 2
            if sudo docker rm -f $container_name 2>/dev/null; then
                # Double check if container is really gone after second attempt
                sleep 1
                if sudo docker ps -a --format "{{.Names}}" | grep -q "^${container_name}$"; then
                    echo "Warning: Container $container_name still exists after second attempt"
                    failed_containers+=($container_name)
                else
                    echo "Container $container_name stopped and removed successfully on second attempt"
                fi
            else
                echo "Warning: Failed to remove container $container_name after two attempts"
                failed_containers+=($container_name)
            fi
        fi
    else
        echo "Container $container_name does not exist (already removed or never created)"
    fi
done




