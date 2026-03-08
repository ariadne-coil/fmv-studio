import ffmpeg
import os
from typing import List

def compile_music_video(video_paths: List[str], audio_path: str, output_path: str) -> str:
    """
    Takes a list of video clip paths, concatenates them, and overlays the given audio track.
    Saves the final video to `output_path`.
    """
    if not video_paths:
        raise ValueError("No video clips provided for compilation.")

    try:
        # Create input streams for all videos
        video_streams = [ffmpeg.input(p) for p in video_paths]
        
        # Concatenate video streams
        concat_videos = ffmpeg.concat(*video_streams, v=1, a=0)
        
        if audio_path and os.path.exists(audio_path):
            audio_stream = ffmpeg.input(audio_path)
            # Combine concatenated video with the audio track
            final_stream = ffmpeg.output(
                concat_videos, audio_stream, output_path, 
                vcodec='libx264', acodec='aac', 
                shortest=None,  # Or set a specific duration policy if videos > audio
                loglevel="error"
            )
        else:
             # Just combine videos without audio
             final_stream = ffmpeg.output(
                concat_videos, output_path, 
                vcodec='libx264',
                loglevel="error"
            )

        ffmpeg.run(final_stream, overwrite_output=True)
        return output_path

    except ffmpeg.Error as e:
        print(f"FFmpeg error: {e.stderr}")
        raise RuntimeError(f"Failed to compile video: {e}")
