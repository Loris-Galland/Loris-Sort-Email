import typer
from rich.console import Console

from mail_sorter.auth import auth_app

app = typer.Typer(
    name="mail-sorter",
    help="Sort and clean your Outlook inbox using a local AI model.",
    add_completion=False,
)
console = Console()

app.add_typer(auth_app, name="auth", help="Login / logout from Microsoft Outlook")


@app.command("index")
def index(
    force: bool = typer.Option(False, "--force", "-f", help="Re-index already stored emails"),
) -> None:
    """[Phase 2] Fetch email metadata from Outlook and store in SQLite."""
    console.print("[yellow]Phase 2 not implemented yet.[/yellow]")
    raise typer.Exit(0)


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
