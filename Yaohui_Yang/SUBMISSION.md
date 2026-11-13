# 提交到 Wi-Fi Tracking 仓库

仓库：[ciott-wifi-capstone/wifi-tracking](https://github.com/ciott-wifi-capstone/wifi-tracking)  
贡献指南：[STUDENT_CONTRIBUTION_GUIDE.md](https://github.com/ciott-wifi-capstone/wifi-tracking/blob/main/STUDENT_CONTRIBUTION_GUIDE.md)

贡献指南要求在个人目录里提交，并用独立分支开 Pull Request，由导师审核合并。指南里的仓库链接是示例；下面的命令用你实际打开的 wifi-tracking 仓库。

| 要命名的东西 | 建议名称 |
| --- | --- |
| 仓库 | wifi-tracking（使用现有仓库） |
| 个人目录 | Yaohui_Yang |
| 分支 | yaohui-yang/initial-submission |
| Commit | Add Yaohui Yang Wi-Fi tracking capstone |
| PR 标题 | Add Yaohui Yang's ESP32-S3 Wi-Fi tracking project |

1. 在 Windows 上登录 GitHub、接受仓库邀请，并安装 [Git for Windows](https://git-scm.com/download/win)。ZIP 解压后会有一个 Yaohui_Yang 文件夹。如果从未配置过 Git 的提交身份，先运行 git config --global user.name "Yaohui Yang"，再用 git config --global user.email 设置你的 GitHub 提交邮箱或 GitHub 的 noreply 邮箱。
2. 在存放项目的文件夹打开 PowerShell，克隆仓库并建分支。若已克隆，在已有仓库内先确认工作区干净，再运行从 git switch main 开始的命令。

~~~powershell
git clone https://github.com/ciott-wifi-capstone/wifi-tracking.git
cd wifi-tracking
git switch main
git pull --ff-only origin main
git switch -c yaohui-yang/initial-submission
~~~

3. 在资源管理器里把解压出的 **Yaohui_Yang 文件夹**复制到 wifi-tracking 根目录。它应和 STUDENT_CONTRIBUTION_GUIDE.md 并列。提交展开后的源文件。
4. 回到 PowerShell，暂存个人目录并查看列表。列表里只应出现 Yaohui_Yang/ 下的文件。确认后提交并推送个人分支：

~~~powershell
git add -- Yaohui_Yang/
git diff --cached --stat
git commit -m "Add Yaohui Yang Wi-Fi tracking capstone"
git push -u origin yaohui-yang/initial-submission
~~~

5. 回到 GitHub，点 **Compare & pull request**，核对 **base = main**、**compare = yaohui-yang/initial-submission**。填写上表标题，正文可用：

> Adds my ELEC5021 project under Yaohui_Yang/. It includes ESP32-S3 firmware, single-link CIR ranging, six-anchor 2D positioning and GRU V2 training and inference. The README lists offline tests and hardware setup.

点 **Create pull request**，请导师审核。若 push 提示无权限，先确认已接受仓库邀请并登录正确账号，再向导师确认是否需要从 fork 发 PR。
