"""Script to run & evaluate agent-loop on a single example from the benchmark."""
import datetime
import json
import logging
import cv2
import numpy as np
import base64
import time
import shutil
from trajectory_recorder import TrajectoryRecorder
from check_setup import check_setup_state_has_error

logger = logging.getLogger("desktopenv.experiment")


def run_single_example(agent, env, example, max_steps, domain, example_id, instruction, args, example_result_dir, scores):
    agent.reset()
    try:
        obs = env.reset(domain=domain, task_config=example)
    except Exception as e:
        if args.check_setup:
            error_message = str(e)
            print(f"Fail to setup environment: {error_message}")
            # remove example_result_dir
            shutil.rmtree(example_result_dir)
            # exit the program
            exit(1)
        else:
            raise e
    
    done = False
    step_idx = 0

    # check if the setup is successful 
    if args.check_setup:
        if obs is None or check_setup_state_has_error(domain, obs["screenshot"]): 
            print("Setup environment finished, but error detected.")
            # remove example_result_dir
            shutil.rmtree(example_result_dir)
            # exit the program
            exit(1)
            
    # Initialize recorder, which will save the trajectory as a JSON & HTML in {example_result_dir}/traj.(jsonl,html)
    recorder = TrajectoryRecorder(example_result_dir)

    scores = []   
    while not done and step_idx < max_steps:
        if obs is None:
            raise Exception("Observation is None.")
            
        response, actions, logs, computer_update_args = agent.predict(
            instruction,
            obs
        )

        # update the computer object, used by navi's action space
        if computer_update_args:
            env.controller.update_computer(**computer_update_args)
        
        # step environment with agent actions 
        for action in actions:
            logger.info("\n\nStep %d: %s", step_idx + 1, action)

            # Capture the timestamp before executing the action
            action_timestamp = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")

            # Record step data
            recorder.record_step(
                obs, 
                logs,
                step_idx,
                action_timestamp,
                action
            )
            obs, reward, done, info = env.step(action, args.sleep_after_execution)

            if done:
                logger.info("The episode is done.")
                break
        # inc step counter
        step_idx += 1

    logger.info("Running evaluator(s)...")
    result = env.evaluate()
    logger.info("Result: %.2f", result)
    scores.append((step_idx, result))

    # Record final results
    final_timestamp = datetime.datetime.now().strftime("%Y%m%d@%H%M%S")
    recorder.record_end(scores, obs, step_idx, final_timestamp)
