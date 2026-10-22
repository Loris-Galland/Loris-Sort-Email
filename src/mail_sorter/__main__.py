import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from mail_sorter.auth import auth_app
from mail_sorter.db import Email, get_session, init_db, sender_stats
from mail_sorter.graph import GraphClient, GraphError, NotLoggedInError

app = typer.Typer(
    name="mail-sorter",
    help="Sort and clean your Outlook inbox using a local AI model.",
    add_completion=False,
)
console = Console()

app.add_typer(auth_app, name="auth", help="Login / logout from Microsoft Outlook")


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


@app.command("index")
def index(
    force: bool = typer.Option(
        False, "--force", "-f", help="Clear local data (including classifications) first"
    ),
) -> None:
    """Fetch inbox metadata from Outlook and store it in SQLite."""
    from mail_sorter.processor import index_inbox

    init_db()
    client = _graph_client()
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
        f"[green]Done.[/green] {stats.added} new, {stats.updated} refreshed, "
        f"{stats.gone} no longer in the inbox."
    )
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


@app.command("classify")
def classify(
    batch_size: int = typer.Option(30, "--batch-size", "-b", help="Emails per Ollama request"),
) -> None:
    """[Phase 3] Classify emails using a local Ollama model."""
    console.print("[yellow]Phase 3 not implemented yet.[/yellow]")
    raise typer.Exit(0)


@app.command("demo")
def demo() -> None:
    """Generate 1000 fake emails to test the dashboard without an Outlook account."""
    console.print("[yellow]Demo mode not implemented yet.[/yellow]")
    raise typer.Exit(0)


if __name__ == "__main__":
    app()
