import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from mail_sorter.auth import auth_app
from mail_sorter.db import IN_INBOX, ActionLog, Email, get_session, init_db, sender_stats
from mail_sorter.graph import GraphClient, GraphError, NotLoggedInError

app = typer.Typer(
    name="mail-sorter",
    help="Sort and clean your Outlook inbox using a local AI model.",
    add_completion=False,
)
console = Console()

rules_app = typer.Typer(help="Sort emails into folders by sender, using rules.toml")

app.add_typer(auth_app, name="auth", help="Login / logout from Microsoft Outlook")
app.add_typer(rules_app, name="rules", help="Sort emails into folders by sender")


def _progress() -> Progress:
    return Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )


def _graph_client() -> GraphClient:
    client = GraphClient()
    try:
        client.inbox_count()
    except NotLoggedInError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except GraphError as exc:
        console.print(f"[red]Graph API error:[/red] {exc}")
        raise typer.Exit(1) from exc
    return client


def _size(size_bytes: int) -> str:
    return f"{size_bytes / 1_048_576:.1f} MB"


def _sync(client: GraphClient, force: bool = False) -> None:
    from mail_sorter.processor import index_inbox

    total = client.inbox_count()
    with get_session() as session:
        if force:
            session.query(Email).delete()
        with _progress() as progress:
            task = progress.add_task("Indexing inbox", total=total)
            try:
                stats = index_inbox(client, session, lambda: progress.advance(task))
            except GraphError as exc:
                console.print(f"[red]Graph API error:[/red] {exc}")
                raise typer.Exit(1) from exc

    console.print(
        f"[green]Indexed.[/green] {stats.added} new, {stats.updated} refreshed, "
        f"{stats.gone} no longer in the inbox."
    )


@app.command("index")
def index(
    force: bool = typer.Option(
        False, "--force", "-f", help="Clear local data (including classifications) first"
    ),
) -> None:
    """Fetch inbox metadata from Outlook and store it in SQLite."""
    init_db()
    _sync(_graph_client(), force)
    console.print("Next: mail-sorter senders")


@app.command("senders")
def senders(
    by: str = typer.Option("domain", "--by", help="Group by 'domain' or 'email'"),
    limit: int = typer.Option(30, "--limit", "-n", help="Number of rows to show"),
) -> None:
    """Show who fills your inbox the most."""
    if by not in ("domain", "email"):
        console.print("[red]--by must be 'domain' or 'email'[/red]")
        raise typer.Exit(1)

    init_db()
    with get_session() as session:
        rows = sender_stats(session, by)

    if not rows:
        console.print("No emails indexed yet. Run: mail-sorter index")
        raise typer.Exit(0)

    table = Table(title=f"Top senders by {by} ({sum(r['count'] for r in rows)} emails in inbox)")
    table.add_column("Sender", style="cyan")
    table.add_column("Name")
    table.add_column("Emails", justify="right")
    table.add_column("Unread", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Latest")
    for row in rows[:limit]:
        latest = row.get("latest")
        table.add_row(
            row["sender"] or "(unknown)",
            row["name"] or "",
            str(row["count"]),
            str(row["unread"]),
            _size(row["size_bytes"]),
            latest.strftime("%Y-%m-%d") if latest else "",
        )
    console.print(table)


@rules_app.command("init")
def rules_init() -> None:
    """Create rules.toml with an example and your biggest senders as suggestions."""
    from mail_sorter.rules import RULES_PATH, build_template

    if RULES_PATH.exists():
        console.print(f"{RULES_PATH} already exists, edit it directly.")
        raise typer.Exit(0)

    init_db()
    with get_session() as session:
        top = [(r["sender"], r["count"]) for r in sender_stats(session)[:15] if r["sender"]]
    RULES_PATH.write_text(build_template(top), encoding="utf-8")
    console.print(
        f"[green]Created {RULES_PATH}[/green]. Edit it, then run: mail-sorter rules apply"
    )


def _rules_preview(matches) -> int:
    table = Table(title="Rules preview")
    table.add_column("#", justify="right")
    table.add_column("Senders", style="cyan")
    table.add_column("Action")
    table.add_column("Emails", justify="right")
    for i, (rule, emails) in enumerate(matches, start=1):
        action = "[red]delete (trash)[/red]" if rule.delete else f"move to {rule.folder}"
        table.add_row(str(i), ", ".join(rule.senders), action, str(len(emails)))
    console.print(table)
    return sum(len(emails) for _, emails in matches)


@rules_app.command("apply")
def rules_apply(
    execute: bool = typer.Option(
        False, "--execute", help="Actually move/delete emails (default is a preview)"
    ),
) -> None:
    """Preview, then apply, the rules from rules.toml to the inbox."""
    from mail_sorter.actions import delete_emails, move_emails
    from mail_sorter.rules import RuleError, load_rules, match_rules

    try:
        rules = load_rules()
    except RuleError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    init_db()
    client = _graph_client() if execute else None
    if client:
        # rules must run on fresh data, the inbox may have changed since the last index
        _sync(client)

    with get_session() as session:
        inbox = session.query(Email).filter(Email.status.in_(IN_INBOX)).all()
        if not inbox:
            console.print("No emails indexed yet. Run: mail-sorter index")
            raise typer.Exit(0)

        matches = match_rules(inbox, rules)
        total = _rules_preview(matches)
        if total == 0:
            console.print("No email matches your rules.")
            raise typer.Exit(0)
        if not execute:
            console.print(f"{total} emails would be affected. Run with --execute to apply.")
            raise typer.Exit(0)

        to_delete = sum(len(emails) for rule, emails in matches if rule.delete)
        question = f"Apply these rules to {total} emails"
        if to_delete:
            question += f" ({to_delete} will go to the trash)"
        if not typer.confirm(question + "?"):
            raise typer.Exit(0)

        with _progress() as progress:
            for rule, emails in matches:
                if not emails:
                    continue
                task = progress.add_task(rule.label, total=len(emails))

                def advance(n: int, task=task) -> None:
                    progress.advance(task, n)

                try:
                    if rule.delete:
                        label = ",".join(rule.senders)
                        result = delete_emails(client, session, emails, label, advance)
                    else:
                        result = move_emails(client, session, emails, rule.folder, advance)
                except GraphError as exc:
                    console.print(f"[red]Graph API error on {rule.label}:[/red] {exc}")
                    continue
                if result.failed:
                    console.print(
                        f"[yellow]{result.failed} emails could not be processed.[/yellow]"
                    )
                # commit after each rule so a later failure does not lose earlier progress
                session.commit()

    console.print(
        "[green]Done.[/green] Undo with: mail-sorter history, then mail-sorter restore <id>"
    )


@app.command("history")
def history(limit: int = typer.Option(20, "--limit", "-n")) -> None:
    """List past move/delete/restore actions."""
    init_db()
    with get_session() as session:
        logs = session.query(ActionLog).order_by(ActionLog.id.desc()).limit(limit).all()

    if not logs:
        console.print("No action yet.")
        raise typer.Exit(0)

    table = Table(title="Action history")
    table.add_column("Id", justify="right")
    table.add_column("Date (UTC)")
    table.add_column("Action")
    table.add_column("Target")
    table.add_column("Emails", justify="right")
    for log in logs:
        table.add_row(
            str(log.id),
            log.timestamp.strftime("%Y-%m-%d %H:%M") if log.timestamp else "",
            log.action,
            log.category or "",
            str(log.count),
        )
    console.print(table)


@app.command("restore")
def restore_cmd(log_id: int = typer.Argument(..., help="Id from 'mail-sorter history'")) -> None:
    """Move the emails of a past move/delete action back to the inbox."""
    from mail_sorter.actions import restore

    init_db()
    client = _graph_client()
    with get_session() as session:
        log = session.get(ActionLog, log_id)
        if log is None:
            console.print(f"[red]No action with id {log_id}[/red]")
            raise typer.Exit(1)
        if not typer.confirm(f"Move {log.count} emails back to the inbox?"):
            raise typer.Exit(0)
        with _progress() as progress:
            task = progress.add_task("Restoring", total=log.count)
            try:
                result = restore(client, session, log_id, lambda n: progress.advance(task, n))
            except (ValueError, GraphError) as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc

    console.print(f"[green]{result.succeeded} restored[/green], {result.failed} failed.")


@app.command("classify")
def classify(
    batch_size: int = typer.Option(30, "--batch-size", "-b", help="Emails per Ollama request"),
    model: str = typer.Option(None, "--model", "-m", help="Ollama model (default: OLLAMA_MODEL)"),
    limit: int = typer.Option(0, "--limit", "-n", help="Only classify this many emails (0 = all)"),
    redo: bool = typer.Option(False, "--all", help="Also re-classify already classified emails"),
) -> None:
    """Classify inbox emails into categories using a local Ollama model."""
    import httpx

    from mail_sorter.classifier import OLLAMA_MODEL, OllamaError, check_ollama, classify_emails

    model = model or OLLAMA_MODEL
    http = httpx.Client()
    try:
        check_ollama(http, model)
    except OllamaError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    init_db()
    with get_session() as session:
        statuses = IN_INBOX if redo else ("pending",)
        query = session.query(Email).filter(Email.status.in_(statuses))
        query = query.order_by(Email.received_at.desc())
        emails = query.limit(limit).all() if limit else query.all()
        if not emails:
            console.print("Nothing to classify. Run 'mail-sorter index' first, or use --all.")
            raise typer.Exit(0)

        console.print(f"Classifying {len(emails)} emails with {model} (Ctrl+C stops safely).")
        with _progress() as progress:
            task = progress.add_task("Classifying", total=len(emails))
            try:
                stats = classify_emails(
                    session, http, emails, batch_size, model, lambda n: progress.advance(task, n)
                )
            except httpx.HTTPError as exc:
                console.print(f"[red]Ollama error:[/red] {exc}")
                raise typer.Exit(1) from exc

    console.print(f"[green]{stats.classified} classified[/green], {stats.failed} failed.")
    if stats.failed:
        console.print("Failed emails stay pending, run the command again to retry them.")
    console.print("Next: uv run streamlit run ui/app.py")


@app.command("demo")
def demo(count: int = typer.Option(1000, "--count", "-n")) -> None:
    """Create a separate demo database to try the dashboard without an Outlook account."""
    from mail_sorter import db
    from mail_sorter.demo import DEMO_DB_PATH, generate_emails

    db.configure(DEMO_DB_PATH)
    db.Base.metadata.drop_all(db.get_engine())
    init_db()
    with get_session() as session:
        session.add_all(Email(**data) for data in generate_emails(count))

    console.print(f"[green]{count} fake emails written to {DEMO_DB_PATH}[/green]")
    console.print("Open the dashboard with: uv run streamlit run ui/app.py -- --demo")


if __name__ == "__main__":
    app()
