import os
import re
from pathlib import Path

def rename_mp4_files_by_camera(directory):
    # 获取目录下所有MP4文件
    mp4_files = list(Path(directory).glob("observation.*.mp4"))
    
    # 按摄像头类型分组
    camera_groups = {}
    for file_path in mp4_files:
        match = re.search(r"observation\.images\.(\w+)_episode_\d+\.mp4", file_path.name)
        if match:
            camera = match.group(1)
            if camera not in camera_groups:
                camera_groups[camera] = []
            camera_groups[camera].append(file_path)
    
    # 对每个摄像头组单独排序和重命名
    for camera, files in camera_groups.items():
        # 按原始序号排序
        files.sort(key=lambda x: int(re.search(r"_episode_(\d+)\.mp4", x.name).group(1)))
        
        # 重新编号（从000000开始）
        counter = 0
        for old_path in files:
            # 生成新文件名
            new_name = re.sub(
                r"_episode_\d+\.mp4", 
                f"_episode_{counter:06d}.mp4", 
                old_path.name
            )
            new_path = old_path.parent / new_name
            
            # 执行重命名
            print(f"Renaming {camera}: {old_path.name} -> {new_name}")
            os.rename(old_path, new_path)
            counter += 1

if __name__ == "__main__":
    video_dir = "/home/qk/lerobot-zhou/data/cheney/dish/videos"  # 替换为你的实际路径
    rename_mp4_files_by_camera(video_dir)
    print("所有摄像头视频文件已独立重新编号！")