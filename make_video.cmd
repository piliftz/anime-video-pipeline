@echo off
rem One-command render on Windows:  make_video my_story.json [--draft] [--force-images] [...]
rem Generates/refreshes scene images with local ComfyUI (auto-started from %COMFYUI_DIR%), then renders.
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\video_orchestrator.py" %* --generate-images
