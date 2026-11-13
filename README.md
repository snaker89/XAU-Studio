# XAU Studio

A Windows desktop application for managing Xbox accounts and resumable, title-based achievement processing.

## Features

- Separate, persistent progress for each Xbox account.
- Microsoft sign-in and locally protected refresh sessions.
- Start, pause, resume, and optional continuation when opening the application.
- Optional randomized title order; each game's achievements are processed together.
- Read-after-write verification, rate-limit handling, bounded retries, and excluded titles.
- A per-run counter of newly completed games, excluding games already complete before processing.
- Search, status filters, CSV import/export, and an activity log.

## Requirements and usage

Windows 10/11 x64, an internet connection, and Microsoft Edge WebView2 Runtime are required. A future portable release will bundle Python and the .NET runtime. The Xbox desktop app is not needed for this application's direct sign-in flow.

Sign in to your account, import an authorized CSV catalogue with `ID` and optional `Game Title` columns, and start processing. Accounts and progress are stored under `%LOCALAPPDATA%\XauStudioPortable\data`, outside the executable. Updating the executable does not erase those files. Each Start/Resume begins a new run counter; completed-title history persists.

Only supported title-based achievements are implemented. Event-based achievements and Xbox 360 unlocking are not supported. Service availability and authorization depend on Microsoft. Using third-party achievement modification tools can violate platform rules and may lead to account enforcement. This project is not affiliated with Microsoft or Xbox.

## Add your own title list

The application starts with an empty catalogue. No separate download of a title list is supplied or required by the repository. Obtain a list yourself from a source you are permitted to use.

1. Create a UTF-8, comma-separated `.csv` file with the exact header `ID,Game Title`.
2. Put one title per row. `ID` is the Xbox **Title ID**, a decimal number from 1 to 4294967295. It is not a Store product ID, achievement ID, or account ID. `Game Title` is an optional display name. Quote names containing commas.
3. Start XAU Studio and select **+ Sign in** to sign in to the intended account.
4. Open **Settings** and click **Add titles from CSV**. Select your file.
5. Click **Start / Resume**. Imported titles are queued and displayed when the run starts. If the queue is empty, no Xbox requests are made.

Format example only — these IDs are placeholders, not a usable game catalogue:

```csv
ID,Game Title
123,Example title
456,"Example title, with comma"
```

Imports are stored per account in `%LOCALAPPDATA%\XauStudioPortable\data\<Xbox-account-ID>\additional-titles.json`. **Use the CSV import button; you do not need to create or edit that JSON file.** Importing again adds new IDs without resetting existing completed-game statuses. Completed titles are skipped on subsequent runs. To use a list on a different account, select that account and import it there as well.

Do not place lists in the runtime extraction directory or commit your private lists, account files, tokens, or databases to this repository. The historical manifest filename referenced in the code is optional compatibility support; it is not needed for CSV import and is not distributed here.

## Build and tests

Install the .NET 9 SDK and Python 3.12 on Windows. Run:

```powershell
dotnet build studio/XauStudio.csproj -c Release
python -m unittest discover -s studio -p "test_*.py"
python -m unittest discover -s automation -p test_runner.py
```

Start the source version with `./Start-XAU-Studio.ps1`. Python 3.12 must be available as `python.exe` on PATH. The mock tests do not unlock real achievements. A portable executable is not part of this initial source publication; release packaging remains tracked in `RELEASE-CHECKLIST.md`.

## Origins and license

XAU Studio is a derivative of [Xbox Achievement Unlocker](https://github.com/Fumo-Unlockers/Xbox-Achievement-Unlocker), based on commit `d8c2e40370eab32aec06a273f4f1cac6fe18a8a8`. Authentication code includes [XboxAuthNet](https://github.com/AlphaBs/XboxAuthNet). Changes made in November 2026 add the Studio interface, direct service integration, account storage, queue orchestration, diagnostics, and portable packaging.

Distributed under GNU GPL version 3; see `LICENSE`. Third-party components retain their original licenses and notices, listed in `THIRD-PARTY-NOTICES.md` and `LICENSES/`.
