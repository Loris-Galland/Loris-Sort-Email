# mail-sorter

> **Work in progress** — project is still being built, not all phases are done yet.

## What is it?

At some point I looked at my inbox and it had built up to something like 10k+ emails — newsletters, promos, invoices, notifications, all piled up with zero organisation. I had absolutely no intention of going through them manually one by one, so I decided to automate it instead.

The idea is simple: a local AI model classifies everything by category, then a dashboard lets me review and delete in bulk. No data leaves the machine, nothing paid, nothing phoning home. It reads only metadata — sender, subject, date — never the email body or attachments.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) — `pip install uv`
- [Ollama](https://ollama.ai) with a model pulled:
  ```
  ollama pull mistral    # 4 GB, recommended
  ollama pull phi3       # 2 GB, faster on CPU-only machines
  ```
- A Microsoft account (Outlook / Office 365)

## Installation

```bash
git clone https://github.com/Loris-Galland/Loris-Sort-Email
cd mail-sorter
uv sync
```

Edit `.env` and fill in your `AZURE_CLIENT_ID` (see Azure setup below).

## Azure App Registration (free, 5 minutes)

You need to register a free application on Azure so the tool can access your emails through Microsoft's official API.

1. Go to [portal.azure.com](https://portal.azure.com) and sign in
2. Search for **App registrations** and open it
3. Click **New registration**
   - Name: anything (e.g. `mail-sorter`)
   - Supported account types: **Personal Microsoft accounts only**
   - Redirect URI: select **Mobile and desktop applications**, enter `http://localhost:8080/callback`
4. Click **Register**
5. Copy the **Application (client) ID** and paste it in `.env` as `AZURE_CLIENT_ID`
6. Go to **API permissions**, verify these delegated permissions are listed:
   - `Mail.Read`
   - `Mail.ReadWrite`
   - `offline_access`

No client secret needed — the app uses OAuth2 PKCE.

## Usage

```bash
mail-sorter auth login      # open browser, log in to Outlook
mail-sorter auth status     # check current login

mail-sorter index           # fetch all email metadata into SQLite
mail-sorter classify        # classify with Ollama (must be running: ollama serve)

uv run streamlit run ui/app.py  # open the validation dashboard
```

Additional options:

```bash
mail-sorter index --force             # re-index even already stored emails
mail-sorter classify --batch-size 10  # smaller batches for low-RAM machines
mail-sorter auth logout
```

## Privacy

| Accessed | Never accessed |
|---|---|
| Sender name and email | Email body |
| Subject line | Attachments |
| Date, size, importance | Contacts or calendar |
| Whether attachments exist (true/false) | Any remote server |

The AI model (Ollama) runs entirely on your machine.
Tokens are encrypted by your OS (Windows Credential Manager).
The SQLite database stays local.

## Development

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pytest tests/ -v
```
