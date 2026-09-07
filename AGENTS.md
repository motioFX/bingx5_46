# bingx5_46 AI Agent Rules & Guidelines

## 1. Deployment & Code Synchronization Rule
- **Never directly overwrite/SCP code files (.py) from Windows to VPS.**
- Always follow the Git-based deployment workflow:
  1. Make and test code changes locally on Windows.
  2. Commit and push to GitHub (`git add`, `git commit`, `git push origin main`).
  3. SSH into the VPS and pull the latest code (`cd /home/ubuntu/bingx5_46 && git pull origin main`).
  4. Safely verify/restart the bot process on the VPS.

## 2. Cross-Platform Compatibility (Windows & Linux)
- Ensure all Python scripts run cleanly from Windows Command Prompt (`cmd.exe`) and PowerShell without unhandled exceptions, encoding crashes, or OS-specific path issues.
- All file IO operations must explicitly declare `encoding="utf-8"`.
- Use `pathlib.Path` for cross-platform path handling.
- Background tasks (like Technocore Agent) and external APIs must be wrapped in fail-safe exception blocks (`except (TechnocoreError, BaseException):`) so that server downtimes (HTTP 503, timeouts) never terminate the main trading bot process.

## 3. Implementation Plan & Walkthrough Rule
- ALWAYS write the Implementation Plan and Walkthrough artifacts entirely in Japanese.
