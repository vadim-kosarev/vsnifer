call .venv\Scripts\activate.bat
@echo on

call python .\vk_vsf_bot.py download --all-channels --since 2026-04-20 --count 200
call python check_ad.py update
call python check_ad.py update-nudes

start python join_video.py --last-full-days 7 --sort interest-asc --orientation vertical   --audio-delay-ms 0
start python join_video.py --last-full-days 7 --sort interest-asc --orientation horizontal --audio-delay-ms 0
