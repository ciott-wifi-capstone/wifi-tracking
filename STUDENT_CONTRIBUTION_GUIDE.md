# Student Code Submission Guide

This guide explains how students contribute code to the **`ciott_wifi_capstone`** GitHub organization using **branches and pull requests (PRs)**. Please **do not push directly to `main`**.

## 1. Find your repository

Choose the repository assigned by your supervisor:

- **Sensing:** `https://github.com/ciott_wifi_capstone/sensing`
- **Tracking:** `https://github.com/ciott_wifi_capstone/tracking`

> These links work after the organization and repositories have been created. You must have access to a private repository before you can clone it.

Each student keeps their code in a folder named after them, for example:

```text
sensing/
├── README.md
├── Zhang_San/
│   ├── README.md
│   ├── requirements.txt
│   └── src/
└── Li_Si/
    └── README.md
```

**Only edit your own folder.** If you need to change shared files or another student's code, ask your supervisor first.

## 2. First-time setup

1. Create a GitHub account and accept the invitation to the repository.
2. Install [Git](https://git-scm.com/downloads).
3. Configure your Git identity (once per computer):

```bash
git config --global user.name "Your Name"
git config --global user.email "your-email@example.com"
```

4. Clone **your assigned** repository (replace `sensing` with `tracking` if needed):

```bash
git clone https://github.com/ciott_wifi_capstone/sensing.git
cd sensing
```

GitHub may ask you to authenticate. Use a supported method such as Git Credential Manager or SSH; do not put passwords or tokens in your code.

## 3. Create a branch for your work

Always start from an up-to-date `main`:

```bash
git switch main
git pull origin main
git switch -c zhang-san/initial-submission
```

Replace `zhang-san` with your own name and `initial-submission` with a short description of the task. Use a **new branch name for each new PR**.

## 4. Add your project files

Create your personal folder and copy your code into it:

```text
Zhang_San/
├── README.md
├── requirements.txt
├── src/
│   └── main.py
└── examples/
```

Your personal `README.md` should explain:

- Project title and objective
- Required software, Python version, and dependencies
- Installation and execution commands
- Input/output data formats and expected results
- Known limitations and contact/contributor information

**Do not upload** passwords, API keys, personal information, large datasets, model checkpoints, or unpublished research materials without explicit approval. Use `.gitignore` for generated files and local environments (for example `.venv/`, `__pycache__/`, and `data/`).

## 5. Commit and push your branch

Check the files you changed:

```bash
git status
git diff
```

Stage **only your folder**, then commit:

```bash
git add Zhang_San/
git commit -m "Add Zhang San capstone project"
git push -u origin zhang-san/initial-submission
```

Replace the example folder and branch names with yours. Before committing, check `git status` again to make sure no unrelated or sensitive files are staged.

## 6. Open a Pull Request (PR)

1. Open your repository on GitHub.
2. Click **Compare & pull request** (or **Pull requests → New pull request**).
3. Set **base** to `main` and **compare** to your branch (for example `zhang-san/initial-submission`).
4. Enter a clear title, such as `Add Zhang San's WiFi sensing project`.
5. In the description, include:
   - What you added or changed
   - Which folder you modified
   - How the supervisor can run or test it
   - Any known issues or dependencies
6. Click **Create pull request** and request your supervisor's review (or notify them with the PR link).

**Do not merge your own PR.** Wait for your supervisor to review and merge it.

## 7. Respond to review feedback

If your supervisor requests changes, edit your files **on the same branch**, then run:

```bash
git add Zhang_San/
git commit -m "Address PR review feedback"
git push
```

Your existing PR updates automatically. You do **not** need to open another PR.

Once your supervisor approves, they will merge the PR into `main`.

## 8. Start your next update

After the previous PR has been merged:

```bash
git switch main
git pull origin main
git switch -c zhang-san/update-model
```

Make your changes, commit, push, and open a **new PR**. Repeat the process for each update.

## Quick checklist before submitting

- [ ] I am working in the correct repository (`sensing` or `tracking`).
- [ ] I created a branch from the latest `main`.
- [ ] I changed only my own named folder (unless approved).
- [ ] My `README.md` explains how to run the code.
- [ ] My code runs, and I checked the results.
- [ ] I did not upload secrets, private data, or unnecessary large files.
- [ ] I pushed my branch and opened a PR targeting `main`.
- [ ] I requested supervisor review and did not merge the PR myself.

## Troubleshooting

**`Permission denied` or repository not found:** Check that you accepted the GitHub invitation, chose the correct repository, and authenticated correctly.

**`git push` rejected:** Make sure you are pushing your personal branch, not `main`. Check your access rights and ask your supervisor if necessary.

**Accidentally changed someone else's files:** Do not submit the PR until you have reverted those changes. Ask your supervisor if you are unsure how.

**Merge conflicts:** Do not force-push to `main`. Ask your supervisor for help resolving the conflict safely.

---

### Workflow summary

```text
Clone repository (first time)
          ↓
Update local main
          ↓
Create personal branch
          ↓
Edit only your own folder
          ↓
Commit and push branch
          ↓
Open Pull Request → main
          ↓
Supervisor reviews
          ↓
Fix issues if requested
          ↓
Supervisor merges into main
```

> **For supervisors:** This workflow is a team policy, not folder-level access control. Students with repository `Write` permission may still have broader technical access. To enforce review before merge, configure appropriate branch rules where supported by the repository visibility and GitHub plan.
