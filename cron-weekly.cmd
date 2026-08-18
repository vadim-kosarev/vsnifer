call .venv\Scripts\activate.bat
@echo on

set HTTP_PROXY=http://user1:Password123!!@vkosarev.name:10126
set HTTPS_PROXY=http://user1:Password123!!@vkosarev.name:10126
set NO_PROXY=127.0.0.1,localhost,*.local,192.168.1.43,192.168.1.1,192.168.1.99,192.168.1.142,192.168.5.0/24,192.168.6.0/24,192.168.5.190,github.com,.github.com,githubusercontent.com,.githubusercontent.com,brightsky,luigi,starlight
set PYTHONUTF8=1
set MSYS_NO_PATHCONV=1

call python .\vk_vsf_bot.py download --all-channels --since 2026-04-20 --count 200
call python check_ad.py update
call python check_ad.py update-nudes

call python join_video.py --last-full-days 7 --sort interest-asc --orientation horizontal --audio-delay-ms 0
call python join_video.py --last-full-days 7 --sort interest-asc --orientation vertical   --audio-delay-ms 0
