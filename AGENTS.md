# bingx5_46 AI Agent Rules & Guidelines

## 1. Deployment & Code Synchronization Rule
- **Never directly overwrite/SCP code files (.py) from Windows to VPS.**
- **Designated Production VPS (Oracle No.2)**:
  - Host: `158.101.157.117`
  - User: `ubuntu`
  - SSH Key: `C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle2`
  - Command: `ssh -i "C:\Users\user\OneDrive\Desktop\OracleVPS\id_rsa.oracle2" ubuntu@158.101.157.117`
  - Directory: `/home/ubuntu/bingx5_46`
  - Venv: `/home/ubuntu/pybot-env`
- Always follow the Git-based deployment workflow:
  1. Make and test code changes locally on Windows.
  2. Commit and push to GitHub (`git add`, `git commit`, `git push origin main`).
  3. SSH into the VPS and pull the latest code (`cd /home/ubuntu/bingx5_46 && git pull origin main`).
  4. Safely verify/restart the bot process on the VPS.

## 2. Cross-Platform Compatibility (Windows & Linux)
- Ensure all Python scripts run cleanly from Windows Command Prompt (`cmd.exe`) and PowerShell without unhandled exceptions, encoding crashes, or OS-specific path issues.
- All file IO operations must explicitly declare `encoding="utf-8"`.
- Use `pathlib.Path` for cross-platform path handling.
- External APIs and background tasks must be wrapped in fail-safe exception blocks (`except (Exception, BaseException):`) so that server downtimes (HTTP 503, timeouts) never terminate the main trading bot process.

## 3. Implementation Plan & Walkthrough Rule
- ALWAYS write the Implementation Plan and Walkthrough artifacts entirely in Japanese.

## 4. Project Rules & Living Documentation Policy
- **Primary Specification Source**: Always consult and strictly adhere to the trading logic, risk management, and operational rules defined in `PROJECT_RULES.md`.
- **Incremental Rule Refinement**: Whenever the user provides new rules, constraints, or operational requirements during conversations, immediately document and organize them into `PROJECT_RULES.md` (in Japanese).
- **Consistency**: Before implementing or modifying code, verify that all changes are fully consistent with `PROJECT_RULES.md`.
