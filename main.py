"""
Business Credit AI — Main Entry Point
CLI interface for running the server and campaign commands.
"""

import typer
import asyncio
import uvicorn
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

app_cli = typer.Typer(help="Business Credit AI — God Mode")
console = Console()


@app_cli.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Host to bind"),
    port: int = typer.Option(8000, help="Port to listen on"),
    reload: bool = typer.Option(False, help="Auto-reload on code change"),
):
    """Start the web server and dashboard."""
    console.print(Panel.fit(
        "[bold purple]⚡ Business Credit AI — God Mode[/bold purple]\n"
        f"[green]Dashboard:[/green] http://localhost:{port}\n"
        "[yellow]Press Ctrl+C to stop[/yellow]",
        title="Starting Server",
    ))
    uvicorn.run(
        "api.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@app_cli.command()
def seed():
    """Seed the lender database."""
    from database import init_db
    from database.db import get_db_context
    from agents.application_agent import ApplicationAgent

    init_db()
    agent = ApplicationAgent()
    with get_db_context() as db:
        added = agent.seed_lenders(db)
    console.print(f"[green]✓ Seeded {added} lenders[/green]")


@app_cli.command()
def list_lenders(
    category: str = typer.Option("", help="Filter by category"),
    tier: str = typer.Option("", help="Filter by tier"),
):
    """List all lenders in the database."""
    from database import init_db
    from database.db import get_db_context
    from database.models import Lender
    from agents.application_agent import ApplicationAgent

    init_db()
    agent = ApplicationAgent()

    with get_db_context() as db:
        if db.query(Lender).count() == 0:
            agent.seed_lenders(db)

        query = db.query(Lender).filter(Lender.is_active == True)
        if category:
            query = query.filter(Lender.category == category)
        if tier:
            query = query.filter(Lender.tier == tier)
        lenders = query.all()

    table = Table(title=f"Business Credit Lenders ({len(lenders)} total)")
    table.add_column("Name", style="cyan")
    table.add_column("Category", style="white")
    table.add_column("Tier", style="yellow")
    table.add_column("Max Credit", style="green")
    table.add_column("Min Score", style="red")
    table.add_column("Hard Pull", style="magenta")
    table.add_column("Reports To", style="blue")

    for l in lenders:
        bureaus = []
        if l.reports_to_dnb: bureaus.append("D&B")
        if l.reports_to_experian_biz: bureaus.append("Exp")
        if l.reports_to_equifax_biz: bureaus.append("EQ")

        max_credit = ""
        if l.credit_limit_max:
            if l.credit_limit_max >= 1_000_000:
                max_credit = f"${l.credit_limit_max/1_000_000:.1f}M"
            elif l.credit_limit_max >= 1_000:
                max_credit = f"${l.credit_limit_max/1_000:.0f}K"
            else:
                max_credit = f"${l.credit_limit_max:.0f}"

        table.add_row(
            l.name,
            l.category,
            l.tier,
            max_credit,
            str(l.min_personal_credit_score) if l.min_personal_credit_score else "None",
            "Yes" if l.hard_pull else "No",
            ", ".join(bureaus) if bureaus else "—",
        )

    console.print(table)


@app_cli.command()
def apply(
    business_id: int = typer.Argument(help="Business profile ID"),
    dry_run: bool = typer.Option(True, help="Dry run — don't actually submit"),
    max_apps: int = typer.Option(0, help="Max applications (0=unlimited)"),
    category: str = typer.Option("", help="Filter by category (e.g. net30)"),
):
    """Run a credit application campaign for a business."""
    from database import init_db
    from database.db import get_db_context
    from database.models import BusinessProfile
    from agents.application_agent import ApplicationAgent

    init_db()
    agent = ApplicationAgent()

    with get_db_context() as db:
        business = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
        if not business:
            console.print(f"[red]Business ID {business_id} not found[/red]")
            raise typer.Exit(1)

        console.print(Panel.fit(
            f"[cyan]Business:[/cyan] {business.legal_name}\n"
            f"[cyan]Mode:[/cyan] {'🟡 Dry Run' if dry_run else '🔴 LIVE'}\n"
            f"[cyan]Max Apps:[/cyan] {max_apps or 'Unlimited'}\n"
            f"[cyan]Category:[/cyan] {category or 'All'}",
            title="Starting Campaign",
        ))

        if not dry_run:
            typer.confirm("⚠️  LIVE mode will actually submit applications. Continue?", abort=True)

        results = asyncio.run(
            agent.run_campaign(
                business=business,
                db=db,
                max_applications=max_apps or None,
                dry_run=dry_run,
                categories=[category] if category else None,
            )
        )

    table = Table(title="Campaign Results")
    table.add_column("Lender", style="cyan")
    table.add_column("Status", style="white")
    table.add_column("Reference", style="green")
    table.add_column("Notes", style="yellow")

    for r in results.get("applications", []):
        status = str(r.get("status", ""))
        style = "green" if "submitted" in status else "red" if "error" in status else "yellow"
        table.add_row(
            r["lender"],
            f"[{style}]{status}[/{style}]",
            r.get("reference") or "—",
            (r.get("notes") or "")[:60],
        )

    console.print(table)
    console.print(
        f"\n[green]✓ Submitted: {results['submitted']}[/green]  "
        f"[yellow]Skipped: {results['skipped']}[/yellow]  "
        f"[red]Errors: {results['errors']}[/red]"
    )


@app_cli.command()
def plan(business_id: int = typer.Argument(help="Business profile ID")):
    """Generate an AI credit building plan for a business."""
    from database import init_db
    from database.db import get_db_context
    from database.models import BusinessProfile
    from agents.orchestrator import CreditOrchestrator

    init_db()
    orch = CreditOrchestrator()

    with get_db_context() as db:
        business = db.query(BusinessProfile).filter(BusinessProfile.id == business_id).first()
        if not business:
            console.print(f"[red]Business ID {business_id} not found[/red]")
            raise typer.Exit(1)

        console.print(f"[yellow]Generating credit plan for {business.legal_name}...[/yellow]")
        plan_data = orch.generate_credit_building_plan(business)

    console.print(Panel(
        f"[gold1]{plan_data.get('plan_name', 'Credit Plan')}[/gold1]\n\n"
        f"{plan_data.get('summary', '')}\n\n"
        f"[cyan]Estimated credit in 12 months:[/cyan] ${plan_data.get('estimated_credit_available_12mo', 0):,.0f}",
        title="Your Credit Building Plan",
    ))


if __name__ == "__main__":
    app_cli()
