from typing import Annotated
from datetime import datetime
from agent.types import State
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command, interrupt
from langchain_core.tools.base import InjectedToolCallId
from langchain_core.messages import ToolMessage

from agent.db import db, song_retriever, genre_retriever

@tool
def verify_customer_info(customer_id: int, tool_call_id: Annotated[str, InjectedToolCallId] ):
    """Verify customer info given a provided customer ID."""
    if customer_id:
        customer = db.run(f"SELECT * FROM customers WHERE CustomerID = {customer_id};")
        human_response = interrupt({"query": "Please enter your first name, last name, and zip code. Please use the format 'First Last Zip'"})       
        parts = human_response["data"].split()
        if len(parts) != 3:
            tool_message = ToolMessage(
                content="Invalid format. Please provide your first name, last name, and zip code separated by spaces.",
                tool_call_id=tool_call_id,
                name="verify_customer_info"
            )
            return Command(goto='customer', update={"messages": [tool_message]})
            
        first, last, zip = parts
        columns = [column.strip("'") for column in customer.split(", ")]
        stored_first = columns[1]
        stored_last = columns[2]
        stored_zip = columns[8]
        
        if first == stored_first and last == stored_last and zip == stored_zip:
            tool_message = ToolMessage(
                content="Successfully verified customer information",
                tool_call_id=tool_call_id,
                name="verify_customer_info"
            )
            return Command(
                goto='customer',
                update={
                    "messages": [tool_message],
                    "customer_id": customer_id
                },
            )
    tool_message = ToolMessage(
        content="Failed to verify customer information",
        tool_call_id=tool_call_id,
        name="verify_customer_info"
    )
    return Command(goto='customer', update={"messages": [tool_message], "customer_id": None})


@tool
def check_for_songs(song_title):
    """Check if a song exists by its name."""
    return song_retriever.invoke(song_title, k=1)


@tool
def recommend_songs_by_genre(genre, customer_id: int):
    """Get songs by a genre (or similar genres) that the customer hasn't already purchased."""
    if not customer_id:
        return "Error: Customer ID is required to provide personalized recommendations"
    
    docs = genre_retriever.invoke(genre, k=2)
    genre_ids = ", ".join([str(d.metadata['GenreId']) for d in docs])
    
    # Query to get tracks by genre that aren't in customer's purchase history
    query = """
        SELECT DISTINCT t.Name as SongName, ar.Name as ArtistName, al.Title as AlbumName, g.Name as GenreName
        FROM tracks t
        JOIN albums al ON t.AlbumId = al.AlbumId
        JOIN artists ar ON al.ArtistId = ar.ArtistId
        JOIN genres g ON t.GenreId = g.GenreId
        WHERE t.GenreId IN ({})
        AND NOT EXISTS (
            SELECT 1 
            FROM invoice_items ii
            JOIN invoices i ON ii.InvoiceId = i.InvoiceId
            WHERE ii.TrackId = t.TrackId
            AND i.CustomerId = {}
        )
        ORDER BY RANDOM()
        LIMIT 5;
    """.format(genre_ids, customer_id)
    
    return db.run(query, include_columns=True)

@tool
def create_invoice(songs: list[str], customer_id: int, tool_call_id: Annotated[str, InjectedToolCallId]):
    """Create a new invoice for the given songs and customer.
    Args:
        songs: List of song names to purchase
        customer_id: ID of the customer making the purchase
    """
    if not customer_id:
        return "Error: Customer ID is required to create an invoice"
    if not songs:
        return "Error: No songs provided for invoice"
    
    # Find track IDs for the songs
    tracks = []
    for song in songs:
        # Get exact or similar matches for the song
        matches = song_retriever.invoke(song, k=1)
        if matches:
            # Get track info
            track_query = """
                SELECT TrackId, Name, UnitPrice 
                FROM tracks 
                WHERE TrackId = {};
            """.format(matches[0].metadata['TrackId'])
            track = db.run(track_query, include_columns=True)
            columns = track.split(", ")
            track_id = columns[0].split(": ")[1]
            track_name = columns[1].split(": ")[1].strip("'")
            price = columns[2].split(": ")[1].strip("}]")
            if track:
                tracks.append((track_id, track_name, price))
    
    if not tracks:
        return "Error: No matching tracks found for the provided songs"
    
    human_response = interrupt(
        {"query": "You will be purchasing the following songs: {} \nPlease confirm by typing 'yes' or 'no'".format([track[1] for track in tracks])}
    )       
    response = human_response["data"]
    if response.lower() != "yes":
        tool_message = ToolMessage(
            content="Transaction cancelled by customer",
            tool_call_id=tool_call_id,
            name="create_invoice"
        )
        Command(goto='invoice', update={"messages": [tool_message]})

    try:
        # Create new invoice
        invoice_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        invoice_query = """
            INSERT INTO invoices (CustomerId, InvoiceDate, Total)
            VALUES ({}, '{}', {})
            RETURNING InvoiceId;
        """.format(customer_id, invoice_date, sum(float(price) for _, _, price in tracks))
        invoice_id = db.run(invoice_query, include_columns=True)
        invoice_id = invoice_id.split(": ")[1].strip("}]")
        
        # Add invoice lines
        for track_id, _, unit_price in tracks:
            line_query = """
                INSERT INTO invoice_items (InvoiceId, TrackId, UnitPrice, Quantity)
                VALUES ({}, {}, {}, 1);
            """.format(invoice_id, track_id, unit_price)
            db.run(line_query)
        
        # Get invoice details for confirmation
        details_query = """
            SELECT t.Name as SongName, i.UnitPrice, inv.InvoiceDate
            FROM invoice_items i
            JOIN tracks t ON i.TrackId = t.TrackId
            JOIN invoices inv ON i.InvoiceId = inv.InvoiceId
            WHERE inv.InvoiceId = {};
        """.format(invoice_id)
        details = db.run(details_query, include_columns=True)
        rows = details.split("}, {")
        if (len(rows) == len(songs)):
            tool_message = ToolMessage(
                content="Transaction successfully completed!",
                tool_call_id=tool_call_id,
                name="create_invoice"
            )
            return Command(goto='invoice', update={"messages": [tool_message]})
        tool_message = ToolMessage(
            content="Transaction encountered error",
            tool_call_id=tool_call_id,
            name="create_invoice"
        )
        return Command(goto='invoice', update={"messages": [tool_message]})
    except Exception as e:
        return f"Error creating invoice: {str(e)}"

@tool
def check_upsell_eligibility(customer_id: int):
    """Check if the customer meets the conditions to upsell:
    - Has made at least one purchase above $2, OR
    - Has only made one purchase total
    """
    if not customer_id:
        return "Error: Customer ID is required"
    
    query = """
        WITH customer_stats AS (
            SELECT COUNT(*) as total_purchases, MAX(Total) as highest_purchase
            FROM invoices
            WHERE CustomerId = {} AND Total > 0
        )
        SELECT 
            total_purchases, highest_purchase,
            CASE 
                WHEN total_purchases = 1 THEN true  
                WHEN highest_purchase >= 2 THEN true  
                ELSE false
            END as should_upsell
        FROM customer_stats;
    """.format(customer_id)
        
    result = db.run(query, include_columns=True)
    if not result:
        return False
    return True

@tool
def get_recommended_upsells(customer_id: int, tool_call_id: Annotated[str, InjectedToolCallId]):
    """Get recommended upsells based on customer's most recent invoice's most common genre.
    Args:
        customer_id: ID of the customer to get recommendations for
    Returns:
        The most common genre in the customer's most recent invoice
    """
    query = """
        WITH LastInvoice AS (
            SELECT InvoiceId
            FROM invoices
            WHERE CustomerId = {}
            ORDER BY InvoiceDate DESC
            LIMIT 1
        ),
        GenreCounts AS (
            SELECT g.Name as GenreName, COUNT(*) as GenreCount
            FROM LastInvoice li
            JOIN invoice_items ii ON li.InvoiceId = ii.InvoiceId
            JOIN tracks t ON ii.TrackId = t.TrackId
            JOIN genres g ON t.GenreId = g.GenreId
            GROUP BY g.GenreId, g.Name
            ORDER BY GenreCount DESC
            LIMIT 1
        )
        SELECT GenreName, GenreCount
        FROM GenreCounts;
    """.format(customer_id)
    
    result = db.run(query, include_columns=True)
    if result:
        # Result will be in format: "[{GenreName: 'Rock', GenreCount: 3}]"
        genre = result.split("'")[1] if "'" in result else None
        
        tool_message = ToolMessage(
            content=f"Recommended genre for customer {customer_id}: {genre}. Handing off to music agent for recommendations",
            tool_call_id=tool_call_id,
            name="get_recommended_upsells"
        )
        return Command(
            goto='music',
            update={ "messages": [tool_message]},
        )
    tool_message = ToolMessage(
        content=f"No recommended genres found for customer {customer_id}. Handing back to sales to reject upsell.",
        tool_call_id=tool_call_id,
        name="get_recommended_upsells"
    )
    return Command(
        goto='sales',
        update={ "messages": [tool_message]},
    )

@tool
def finalize_upsell_decision(upsell: bool, song: str | None, tool_call_id: Annotated[str, InjectedToolCallId]):
    """Finalize whether or not to upsell the customer. Song must be provided if upselling - if it is not, no upsell will be made"""
    message = "No, do not upsell"
    if upsell and song is not None:
        message = "Yes, upsell {}".format(song)
    tool_message = ToolMessage(
        content=f"Final upsell decision: {message}",
        tool_call_id=tool_call_id,
        name="finalize_upsell_decision"
    )
    return Command(
        goto='invoice',
        update={ "messages": [tool_message]},
    )


def make_handoff_tool(*, agent_name: str):
    """Create a tool that can return handoff via a Command"""
    tool_name = f"transfer_to_{agent_name}"

    @tool(tool_name)
    def handoff(
        tool_call_id: Annotated[str, InjectedToolCallId],
    ):
        """Ask another agent for help."""
        tool_message = ToolMessage(
            content=f"Successfully transferred to {agent_name}",
            name=tool_name,
            tool_call_id=tool_call_id,
        )
        return Command(
            goto=agent_name,
            update={"messages": [tool_message]},
        )
    return handoff