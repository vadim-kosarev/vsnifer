call .venv\Scripts\activate.bat

call python .\vk_vsf_bot.py download --all-channels --since 2026-04-20 --count 200
call python check_ad.py update

python join_video.py --last-days 3d --sort interest-asc --orientation vertical   --audio-delay-ms 0
python join_video.py --last-days 3d --sort interest-asc --orientation horizontal --audio-delay-ms 0
