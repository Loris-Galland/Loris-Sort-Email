# mail-sorter

## What is it?

At some point I looked at my inbox and it had built up to something like 10k+ emails — newsletters, promos, invoices, notifications, all piled up with zero organisation. I had absolutely no intention of going through them manually one by one, so I decided to automate it instead.

The idea is simple: rules sort the big senders into folders (all the Riot Games mail goes to `Jeux/Riot`), a local AI model classifies the rest by category, then a dashboard lets me review, move and delete in bulk. No data leaves the machine, nothing paid, nothing phoning home. It reads only metadata — sender, subject, date — never the email body or attachments.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) — `pip install uv`
- [Ollama](https://ollama.com), only for the AI classification step, with a model pulled:
  ```
  ollama pull mistral    # 4 GB, recommended
  ollama pull phi3       # 2 GB, faster on CPU-only machines
  ```
- A Microsoft account (Outlook / Office 365)

## Installation

```bash
git clone https://github.com/Loris-Galland/Loris-Sort-Email
cd Loris-Sort-Email
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

### Sort visually in the dashboard

```bash
uv run mail-sorter auth login       # once, opens the browser to log in to Outlook
uv run streamlit run ui/app.py      # opens the dashboard in your browser
```

1. Click **Sync inbox** in the sidebar to fetch your inbox (metadata only).
2. **Overview** shows your biggest senders and categories. Click a bar to open it.
3. **Senders** is where the sorting happens. Pick a sender in the table, narrow by age if you want ("older than 6 months"), then:
   - **Move** its emails to a folder (`Jeux/Riot`, created if missing), or
   - **Delete** them (they go to Deleted Items).

   Keep "Also save as a rule" ticked and the same thing will happen to its future emails.
4. **Rules** lists your rules in an editable table. **Apply all rules** sorts everything new in one click, after a confirmation.
5. **Categories** shows what the AI classified (promo, newsletter...), handy to delete in bulk.
6. **History** lists every action and can move the emails back to the inbox.

Every move or delete asks for confirmation first.

**Classify with AI** in the sidebar needs [Ollama](https://ollama.com) running locally with `ollama pull mistral`. Sorting by sender works without it.

Want to look around first without touching your mailbox?

```bash
uv run mail-sorter demo
uv run streamlit run ui/app.py -- --demo
```

The demo uses its own database, rules and backups, so it never touches your real ones.

### Command line

Everything the dashboard does is also available from the terminal:

```bash
uv run mail-sorter index                     # sync the inbox metadata into SQLite
uv run mail-sorter senders                   # who fills your inbox the most
uv run mail-sorter rules init                # create rules.toml from your biggest senders
uv run mail-sorter rules apply               # preview what the rules would do
uv run mail-sorter rules apply --execute     # re-sync, confirm, then move / delete
uv run mail-sorter classify --limit 200      # classify with the local AI
```

`rules.toml` example:

```toml
[[rules]]
folder = "Jeux/Riot"                                # created if missing, "/" for subfolders
senders = ["riotgames.com", "leagueoflegends.com"]  # domains also match their subdomains

[[rules]]
action = "delete"                                   # goes to Deleted Items, not permanent
senders = ["promo@shop.com"]
older_than_days = 30                                # optional
subject_contains = ["promo", "soldes"]              # optional
```

Rules only act on what is in the inbox when you apply them, apply them again from time to time to sort new emails.

For classification, emails with the same sender and subject are classified only once. On a CPU-only machine expect a few seconds per email. `Ctrl+C` stops safely and the next run resumes.

### Undo

Every move or delete writes a JSON backup in `backups/` and can be undone from the History page, or:

```bash
uv run mail-sorter history
uv run mail-sorter restore 3
```

Deleting means moving to Deleted Items, nothing is ever permanently deleted by this tool. Outlook empties Deleted Items on its own schedule, so restore soon if you need to.

### Configuration

Optional variables in `.env`:

| Variable | Default |
|---|---|
| `DB_PATH` | `./mail_sorter.db` |
| `RULES_PATH` | `./rules.toml` |
| `BACKUP_DIR` | `./backups` |
| `OLLAMA_MODEL` | `mistral` |
| `OLLAMA_URL` | `http://localhost:11434` (must stay local, remote hosts are refused) |

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
uv run ruff check src/ tests/ ui/
uv run ruff format src/ tests/ ui/
uv run pytest tests/ -v
```
