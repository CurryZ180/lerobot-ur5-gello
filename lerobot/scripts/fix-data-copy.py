
import argparse
import concurrent.futures
import json
import logging
import os
import platform
import shutil
import time
import traceback
from contextlib import nullcontext
from functools import cache
from pathlib import Path

import cv2
import torch
import tqdm
from omegaconf import DictConfig
from PIL import Image
from termcolor import colored

# from safetensors.torch import load_file, save_file
from lerobot.common.datasets.compute_stats import compute_stats
from lerobot.common.datasets.lerobot_dataset import CODEBASE_VERSION, LeRobotDataset
from lerobot.common.datasets.push_dataset_to_hub.aloha_hdf5_format import to_hf_dataset
from lerobot.common.datasets.push_dataset_to_hub.utils import concatenate_episodes, get_default_encoding
from lerobot.common.datasets.utils import calculate_episode_data_index, create_branch
from lerobot.common.datasets.video_utils import encode_video_frames
from lerobot.common.policies.factory import make_policy
from lerobot.common.robot_devices.robots.factory import make_robot
from lerobot.common.robot_devices.robots.utils import Robot
from lerobot.common.utils.utils import get_safe_torch_device, init_hydra_config, init_logging, set_global_seed
from lerobot.scripts.eval import get_pretrained_policy_path
from lerobot.scripts.push_dataset_to_hub import (
    push_dataset_card_to_hub,
    push_meta_data_to_hub,
    push_videos_to_hub,
    save_meta_data,
)

########################################################################################
# Utilities
########################################################################################


def say(text, blocking=False):
    # Check if mac, linux, or windows.
    if platform.system() == "Darwin":
        cmd = f'say "{text}"'
    elif platform.system() == "Linux":
        cmd = f'spd-say "{text}"'
    elif platform.system() == "Windows":
        cmd = (
            'PowerShell -Command "Add-Type -AssemblyName System.Speech; '
            f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')\""
        )

    if not blocking and platform.system() in ["Darwin", "Linux"]:
        # TODO(rcadene): Make it work for Windows
        # Use the ampersand to run command in the background
        cmd += " &"

    os.system(cmd)


def save_image(img_tensor, key, frame_index, episode_index, videos_dir):
    """
    将输入的张量图像保存到指定路径下。
    
    Args:
        img_tensor (Tensor): 输入的张量图像，形状为 (H, W, C)，数据类型为 float32 或 uint8。
        key (str): 图像保存路径的标识字符串。
        frame_index (int): 当前帧的索引号。
        episode_index (int): 当前片段的索引号。
        videos_dir (Path): 图像保存路径的根目录。
    
    Returns:
        None
    
    """
    img = Image.fromarray(img_tensor.numpy())
    path = videos_dir / f"{key}_episode_{episode_index:06d}" / f"frame_{frame_index:06d}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), quality=100)


def busy_wait(seconds):
    # Significantly more accurate than `time.sleep`, and mendatory for our use case,
    # but it consumes CPU cycles.
    # TODO(rcadene): find an alternative: from python 11, time.sleep is precise
    end_time = time.perf_counter() + seconds
    while time.perf_counter() < end_time:
        pass


def none_or_int(value):
    if value == "None":
        return None
    return int(value)


def log_control_info(robot, dt_s, episode_index=None, frame_index=None, fps=None):
    log_items = []
    if episode_index is not None:
        log_items += [f"ep:{episode_index}"]
    if frame_index is not None:
        log_items += [f"frame:{frame_index}"]

    def log_dt(shortname, dt_val_s):
        nonlocal log_items
        log_items += [f"{shortname}:{dt_val_s * 1000:5.2f} ({1/ dt_val_s:3.1f}hz)"]

    # total step time displayed in milliseconds and its frequency
    log_dt("dt", dt_s)

    for name in robot.leader_arms:
        key = f"read_leader_{name}_pos_dt_s"
        if key in robot.logs:
            log_dt("dtRlead", robot.logs[key])

    for name in robot.follower_arms:
        key = f"write_follower_{name}_goal_pos_dt_s"
        if key in robot.logs:
            log_dt("dtWfoll", robot.logs[key])

        key = f"read_follower_{name}_pos_dt_s"
        if key in robot.logs:
            log_dt("dtRfoll", robot.logs[key])

    for name in robot.cameras:
        key = f"read_camera_{name}_dt_s"
        if key in robot.logs:
            log_dt(f"dtR{name}", robot.logs[key])

    info_str = " ".join(log_items)
    if fps is not None:
        actual_fps = 1 / dt_s
        if actual_fps < fps - 1:
            info_str = colored(info_str, "yellow")
    logging.info(info_str)


@cache
def is_headless():
    """Detects if python is running without a monitor."""
    try:
        import pynput  # noqa

        return False
    except Exception:
        print(
            "Error trying to import pynput. Switching to headless mode. "
            "As a result, the video stream from the cameras won't be shown, "
            "and you won't be able to change the control flow with keyboards. "
            "For more info, see traceback below.\n"
        )
        traceback.print_exc()
        print()
        return True




def record(
    robot: "1",
):
    # TODO(rcadene): Add option to record logs
    # TODO(rcadene): Clean this function via decomposition in higher level functions

    # _, dataset_name = repo_id.split("/")
    # if dataset_name.startswith("eval_") and policy is None:
    #     raise ValueError(
    #         f"Your dataset name begins by 'eval_' ({dataset_name}) but no policy is provided ({policy})."
    #     )

    # if not video:
    #     raise NotImplementedError()

    # if not robot.is_connected:
    #     robot.connect()# todo 

    # local_dir = Path(root) / repo_id
    # if local_dir.exists() and force_override:
    #     shutil.rmtree(local_dir)

    # episodes_dir = local_dir / "episodes"
    # episodes_dir.mkdir(parents=True, exist_ok=True)

    # videos_dir = local_dir / "videos"
    # videos_dir.mkdir(parents=True, exist_ok=True)

    # # Logic to resume data recording
    # rec_info_path = episodes_dir / "data_recording_info.json"
    # if rec_info_path.exists() and policy is None:
    #     with open(rec_info_path) as f:
    #         rec_info = json.load(f)
    #     episode_index = rec_info["last_episode_index"] + 1
    # else:
    #     episode_index = 0

    # if is_headless():
    #     logging.info(
    #         "Headless environment detected. On-screen cameras display and keyboard inputs will not be available."
    #     )

    # # Allow to exit early while recording an episode or resetting the environment,
    # # by tapping the right arrow key '->'. This might require a sudo permission
    # # to allow your terminal to monitor keyboard events.
    # exit_early = False
    # rerecord_episode = False
    # stop_recording = False
    # listener =None
    
    # # Only import pynput if not in a headless environment
    # if not is_headless():
    #     from pynput import keyboard

    #     def on_press(key):
    #         nonlocal exit_early, rerecord_episode, stop_recording
    #         try:
    #             if key == keyboard.Key.right:
    #                 print("Right arrow key pressed. Exiting loop...")
    #                 exit_early = True
    #             elif key == keyboard.Key.left:
    #                 print("Left arrow key pressed. Exiting loop and rerecord the last episode...")
    #                 rerecord_episode = True
    #                 exit_early = True
    #             elif key == keyboard.Key.esc:
    #                 print("Escape key pressed. Stopping data recording...")
    #                 stop_recording = True
    #                 exit_early = True
    #         except Exception as e:
    #             print(f"Error handling key press: {e}")

    #     listener = keyboard.Listener(on_press=on_press)
    #     listener.start()

    # # Load policy if any
    # if policy is not None:
    #     # Check device is available
    #     device = get_safe_torch_device(hydra_cfg.device, log=True)

    #     policy.eval()
    #     policy.to(device)

    #     torch.backends.cudnn.benchmark = True
    #     torch.backends.cuda.matmul.allow_tf32 = True
    #     set_global_seed(hydra_cfg.seed)

    #     # override fps using policy fps
    #     fps = hydra_cfg.env.fps

    # fps = fps if fps is not None else 30
    # # Execute a few seconds without recording data, to give times
    # # to the robot devices to connect and start synchronizing.
    # timestamp = 0
    # start_warmup_t = time.perf_counter()
    # is_warmup_print = False
    # while timestamp < warmup_time_s:
    #     if not is_warmup_print:
    #         logging.info("Warming up (no data recording)")
    #         say("Warming up")
    #         is_warmup_print = True

    #     start_loop_t = time.perf_counter()

    #     if policy is None:
    #         observation, action = robot.teleop_step(record_data=True)
    #     else:
    #         observation = robot.capture_observation()

    #     if not is_headless():
    #         image_keys = [key for key in observation if "image" in key]
    #         for key in image_keys:
    #             print("cv2.imshow")
    #             cv2.imshow(key, cv2.cvtColor(observation[key].numpy(), cv2.COLOR_RGB2BGR))
    #         cv2.waitKey(1)

    #     dt_s = time.perf_counter() - start_loop_t
    #     busy_wait(1 / fps - dt_s)

    #     dt_s = time.perf_counter() - start_loop_t
    #     log_control_info(robot, dt_s, fps=fps)

    #     timestamp = time.perf_counter() - start_warmup_t

    # print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! Start !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    # # Save images using threads to reach high fps (30 and more)
    # # Using `with` to exist smoothly if an execption is raised.
    # # Using only 4 worker threads to avoid blocking the main thread.
    # futures = []
    # image_keys = []
    # not_image_keys = []
    # with concurrent.futures.ThreadPoolExecutor(max_workers=num_image_writers) as executor:
    #     # Start recording all episodes
    #     print("episode_index:",episode_index)
    #     print("num_episodes:",num_episodes)
    #     while episode_index < num_episodes:
    #         logging.info(f"Recording episode {episode_index}")
    #         say(f"Recording episode {episode_index}")
    #         ep_dict = {}
    #         frame_index = 0
    #         timestamp = 0
    #         start_episode_t = time.perf_counter()
    #         while timestamp < episode_time_s:
    #             start_loop_t = time.perf_counter()

    #             if policy is None:
    #                 observation, action = robot.teleop_step(record_data=True)
    #             else:
    #                 observation = robot.capture_observation()

    #             image_keys = [key for key in observation if "image" in key]
    #             not_image_keys = [key for key in observation if "image" not in key]

    #             for key in image_keys:
    #                 futures += [
    #                     executor.submit(
    #                         save_image, observation[key], key, frame_index, episode_index, videos_dir
    #                     )
    #                 ]

    #             if not is_headless():
    #                 image_keys = [key for key in observation if "image" in key]
    #                 for key in image_keys:
    #                     cv2.imshow(key, cv2.cvtColor(observation[key].numpy(), cv2.COLOR_RGB2BGR))
    #                 cv2.waitKey(1)

    #             for key in not_image_keys:
    #                 if key not in ep_dict:
    #                     ep_dict[key] = []
    #                 ep_dict[key].append(observation[key])
                
    #             if policy is not None:
    #                 with (
    #                     torch.inference_mode(),
    #                     torch.autocast(device_type=device.type)
    #                     if device.type == "cuda" and hydra_cfg.use_amp
    #                     else nullcontext(),
    #                 ):
    #                     # Convert to pytorch format: channel first and float32 in [0,1] with batch dimension
    #                     for name in observation:
    #                         if "image" in name:
    #                             observation[name] = observation[name].type(torch.float32) / 255
    #                             observation[name] = observation[name].permute(2, 0, 1).contiguous()
    #                         observation[name] = observation[name].unsqueeze(0)
    #                         observation[name] = observation[name].to(device)

    #                     # Compute the next action with the policy
    #                     # based on the current observation
    #                     action = policy.select_action(observation)

    #                     # Remove batch dimension
    #                     action = action.squeeze(0)

    #                     # Move to cpu, if not already the case
    #                     action = action.to("cpu")

    #                 # Order the robot to move
    #                 robot.send_action(action)
    #                 action = {"action": action}

    #             for key in action:
    #                 if key not in ep_dict:
    #                     ep_dict[key] = []
    #                 ep_dict[key].append(action[key])

    #             frame_index += 1

    #             dt_s = time.perf_counter() - start_loop_t
    #             busy_wait(1 / fps - dt_s)

    #             dt_s = time.perf_counter() - start_loop_t
    #             log_control_info(robot, dt_s, fps=fps)

    #             timestamp = time.perf_counter() - start_episode_t
    #             if exit_early:
    #                 exit_early = False
    #                 break

    #         if not stop_recording:
    #             # Start resetting env while the executor are finishing
    #             logging.info("Reset the environment")
    #             say("Reset the environment")

    #         timestamp = 0
    #         start_vencod_t = time.perf_counter()

    #         # During env reset we save the data and encode the videos
    #         num_frames = frame_index

    #         for key in image_keys:
    #             tmp_imgs_dir = videos_dir / f"{key}_episode_{episode_index:06d}"
    #             fname = f"{key}_episode_{episode_index:06d}.mp4"
    #             video_path = local_dir / "videos" / fname
    #             if video_path.exists():
    #                 video_path.unlink()
    #             # Store the reference to the video frame, even tho the videos are not yet encoded
    #             ep_dict[key] = []
    #             for i in range(num_frames):
    #                 ep_dict[key].append({"path": f"videos/{fname}", "timestamp": i / fps})

    #         for key in not_image_keys:
    #             ep_dict[key] = torch.stack(ep_dict[key])

    #         for key in action:
    #             ep_dict[key] = torch.stack(ep_dict[key])

    #         ep_dict["episode_index"] = torch.tensor([episode_index] * num_frames)
    #         ep_dict["frame_index"] = torch.arange(0, num_frames, 1)
    #         ep_dict["timestamp"] = torch.arange(0, num_frames, 1) / fps

    #         done = torch.zeros(num_frames, dtype=torch.bool)
    #         done[-1] = True
    #         ep_dict["next.done"] = done

    #         ep_path = episodes_dir / f"episode_{episode_index}.pth"
            
    #         torch.save(ep_dict, ep_path)

    #         rec_info = {
    #             "last_episode_index": episode_index,
    #         }
    #         with open(rec_info_path, "w") as f:
    #             json.dump(rec_info, f)

    #         is_last_episode = stop_recording or (episode_index == (num_episodes - 1))
    #         if policy is None:
    #             print(f"保存第{episode_index}帧数据完成。一轮采集数据结束，暂停恢复环境，leader机械臂返回原点。")
    #             input("按回车，开始新一轮恢复采集")
                 
           
    #         if rerecord_episode:
    #             rerecord_episode = False
    #             continue

    #         episode_index += 1

    #         if is_last_episode:
    #             logging.info("Done recording")
    #             say("Done recording", blocking=True)
    #             if not is_headless():
    #                 listener.stop()

    #             logging.info("Waiting for threads writing the images on disk to terminate...")
    #             for _ in tqdm.tqdm(
    #                 concurrent.futures.as_completed(futures), total=len(futures), desc="Writting images"
    #             ):
    #                 pass
    #             break

    # robot.disconnect()
    # if not is_headless():
    #     cv2.destroyAllWindows()

    # num_episodes = episode_index

    # logging.info("Encoding videos")
    # say("Encoding videos")
    # # Use ffmpeg to convert frames stored as png into mp4 videos
    # for episode_index in tqdm.tqdm(range(num_episodes)):
    #     for key in image_keys:
    #         tmp_imgs_dir = videos_dir / f"{key}_episode_{episode_index:06d}"
    #         fname = f"{key}_episode_{episode_index:06d}.mp4"
    #         video_path = local_dir / "videos" / fname
    #         if video_path.exists():
    #             # Skip if video is already encoded. Could be the case when resuming data recording.
    #             continue
    #         # note: `encode_video_frames` is a blocking call. Making it asynchronous shouldn't speedup encoding,
    #         # since video encoding with ffmpeg is already using multithreading.
    #         encode_video_frames(tmp_imgs_dir, video_path, fps, overwrite=True)
    #         shutil.rmtree(tmp_imgs_dir)


    episodes_dir = "data/zhou/dish_4_2_1/episodes"
    episodes_dir =Path(episodes_dir)
    videos_dir = "data/zhou/dish_4_2_1/videos"
    videos_dir = Path(videos_dir)
    local_dir = "data/zhou/dish_4_2_1"
    local_dir = Path(local_dir)
    logging.info("Concatenating episodes")
    run_compute_stats =1
    video =1
    repo_id = "zhou/dish_4_2_1"
    repo_id = Path(repo_id)


    
    ep_dicts = []
    num_episodes =36
    for episode_index in tqdm.tqdm(range(num_episodes)):
        ep_path = episodes_dir / f"episode_{episode_index}.pth"
        ep_dict = torch.load(ep_path)
        ep_dicts.append(ep_dict)
    data_dict = concatenate_episodes(ep_dicts)

    total_frames = data_dict["frame_index"].shape[0]
    data_dict["index"] = torch.arange(0, total_frames, 1)

    hf_dataset = to_hf_dataset(data_dict, video)
    episode_data_index = calculate_episode_data_index(hf_dataset)
    info = {
        "codebase_version": CODEBASE_VERSION,
        "fps": 30,
        "video": video,
    }
    if video:
        info["encoding"] = get_default_encoding()

    lerobot_dataset = LeRobotDataset.from_preloaded(
        repo_id=repo_id,
        hf_dataset=hf_dataset,
        episode_data_index=episode_data_index,
        info=info,
        videos_dir=videos_dir,
    )
    if run_compute_stats:
        logging.info("Computing dataset statistics")
        say("Computing dataset statistics")
        stats = compute_stats(lerobot_dataset)
        lerobot_dataset.stats = stats
    else:
        stats = {}
        logging.info("Skipping computation of the dataset statistics")

    hf_dataset = hf_dataset.with_format(None)  # to remove transforms that cant be saved
    hf_dataset.save_to_disk(str(local_dir / "train"))

    meta_data_dir = local_dir / "meta_data"
    save_meta_data(info, stats, episode_data_index, meta_data_dir)

    # if push_to_hub:
    #     print("----------start push-to-hub---------")
    #     hf_dataset.push_to_hub(repo_id, revision="main")
    #     push_meta_data_to_hub(repo_id, meta_data_dir, revision="main")
    #     push_dataset_card_to_hub(repo_id, revision="main", tags=tags)
    #     if video:
    #         push_videos_to_hub(repo_id, videos_dir, revision="main")
    #     create_branch(repo_id, repo_type="dataset", branch=CODEBASE_VERSION)

    logging.info("Exiting")
    say("Exiting")
    return lerobot_dataset




if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Set common options for all the subparsers
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument(
        "--robot-path",
        type=str,
        # default="lerobot/configs/robot/koch.yaml",
        default="lerobot/configs/robot/gello_ur5.yaml",
        help="Path to robot yaml file used to instantiate the robot using `make_robot` factory function.",
    )
    base_parser.add_argument(
        "--robot-overrides",
        type=str,
        nargs="*",
        help="Any key=value arguments to override config values (use dots for.nested=overrides)",
    )

    parser_calib = subparsers.add_parser("calibrate", parents=[base_parser])

    parser_teleop = subparsers.add_parser("teleoperate", parents=[base_parser])
    parser_teleop.add_argument(
        "--fps", type=none_or_int, default=None, help="Frames per second (set to None to disable)"
    )

    parser_record = subparsers.add_parser("record", parents=[base_parser])
    parser_record.add_argument(
        "--fps", type=none_or_int, default=None, help="Frames per second (set to None to disable)"
    )
    parser_record.add_argument(
        "--root",
        type=Path,
        default="data",
        help="Root directory where the dataset will be stored locally at '{root}/{repo_id}' (e.g. 'data/hf_username/dataset_name').",
    )
    parser_record.add_argument(
        "--repo-id",
        type=str,
        default="lerobot/test",
        help="Dataset identifier. By convention it should match '{hf_username}/{dataset_name}' (e.g. `lerobot/test`).",
    )
    parser_record.add_argument(
        "--warmup-time-s",
        type=int,
        default=2,
        help="Number of seconds before starting data collection. It allows the robot devices to warmup and synchronize.",
    )
    parser_record.add_argument(
        "--episode-time-s",
        type=int,
        default=20,
        help="Number of seconds for data recording for each episode.",
    )
    parser_record.add_argument(
        "--reset-time-s",
        type=int,
        default=20,
        help="Number of seconds for resetting the environment after each episode.",
    )
    parser_record.add_argument("--num-episodes", type=int, default=20, help="Number of episodes to record.")
    parser_record.add_argument(
        "--run-compute-stats",
        type=int,
        default=1,
        help="By default, run the computation of the data statistics at the end of data collection. Compute intensive and not required to just replay an episode.",
    )
    parser_record.add_argument(
        "--push-to-hub",
        type=int,
        default=1,
        help="Upload dataset to Hugging Face hub.",
    )
    parser_record.add_argument(
        "--tags",
        type=str,
        nargs="*",
        help="Add tags to your dataset on the hub.",
    )
    parser_record.add_argument(
        "--num-image-writers",
        type=int,
        default=8,
        help="Number of threads writing the frames as png images on disk. Don't set too much as you might get unstable fps due to main thread being blocked.",
    )
    parser_record.add_argument(
        "--force-override",
        type=int,
        default=0,
        help="By default, data recording is resumed. When set to 1, delete the local directory and start data recording from scratch.",
    )
    parser_record.add_argument(
        "-p",
        "--pretrained-policy-name-or-path",
        type=str,
        help=(
            "Either the repo ID of a model hosted on the Hub or a path to a directory containing weights "
            "saved using `Policy.save_pretrained`."
        ),
    )
    parser_record.add_argument(
        "--policy-overrides",
        type=str,
        nargs="*",
        help="Any key=value arguments to override config values (use dots for.nested=overrides)",
    )

    parser_replay = subparsers.add_parser("replay", parents=[base_parser])
    parser_replay.add_argument(
        "--fps", type=none_or_int, default=None, help="Frames per second (set to None to disable)"
    )
    parser_replay.add_argument(
        "--root",
        type=Path,
        default="data",
        help="Root directory where the dataset will be stored locally at '{root}/{repo_id}' (e.g. 'data/hf_username/dataset_name').",
    )
    parser_replay.add_argument(
        "--repo-id",
        type=str,
        default="lerobot/test",
        help="Dataset identifier. By convention it should match '{hf_username}/{dataset_name}' (e.g. `lerobot/test`).",
    )
    parser_replay.add_argument("--episode", type=int, default=0, help="Index of the episode to replay.")

    parser_eval = subparsers.add_parser("eval", parents=[base_parser])
    parser_eval.add_argument(
        "-p",
        "--pretrained-policy-name-or-path",
        type=str,
        help=(
            "default=outputs/train/act_ur5_test/checkpoints/last/pretrained_model"
        ),
    )
    parser_eval.add_argument(
        "--policy-overrides",
        type=str,
        nargs="*",
        help="Any key=value arguments to override config values (use dots for.nested=overrides)",
    )

    args = parser.parse_args()

    init_logging()

    control_mode = args.mode
    robot_path = args.robot_path
    robot_overrides = args.robot_overrides
    kwargs = vars(args)
    del kwargs["mode"]
    del kwargs["robot_path"]
    del kwargs["robot_overrides"]

    robot_cfg = init_hydra_config(robot_path, robot_overrides)
    # robot = make_robot(robot_cfg)

    robot = "1"
    # pretrained_policy_name_or_path = args.pretrained_policy_name_or_path
    # policy_overrides = args.policy_overrides
    # del kwargs["pretrained_policy_name_or_path"]
    # del kwargs["policy_overrides"]
    
    record(robot)

