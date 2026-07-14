"""
reports.py
================================================================================
VERZIO STUDIO — EXCEL REPORTS GENERATOR
================================================================================
Converts active database tables into formatted Excel sheets for business owners.
"""

import os
from datetime import datetime, date, time
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from sqlalchemy.orm import Session
from database import Appointment

def generate_daily_excel_report(business_id: int, business_name: str, db: Session) -> str:
    """
    Queries the database for today's bookings for a specific business,
    generates a beautifully styled Excel sheet, and returns the local file path.
    """
    # 1. Fetch today's data window
    today_start = datetime.combine(date.today(), time.min)
    today_end = datetime.combine(date.today(), time.max)
    
    appointments = db.query(Appointment).filter(
        Appointment.business_id == business_id,
        Appointment.appointment_time >= today_start,
        Appointment.appointment_time <= today_end
    ).order_by(Appointment.appointment_time.asc()).all()

    # 2. Initialize openpyxl Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Today's Bookings"
    
    # Ensure grid lines are visible
    ws.views.sheetView[0].showGridLines = True

    # 3. Styling Definitions (Professional Minimalist Palette)
    font_family = "Segoe UI"
    header_font = Font(name=font_family, size=11, bold=True, color="FFFFFF")
    title_font = Font(name=font_family, size=16, bold=True, color="1E293B")
    meta_font = Font(name=font_family, size=10, italic=True, color="64748B")
    data_font = Font(name=font_family, size=11, color="334155")
    
    header_fill = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid") # Indigo Accent
    
    thin_border = Border(
        left=Side(style='thin', color='E2E8F0'),
        right=Side(style='thin', color='E2E8F0'),
        top=Side(style='thin', color='E2E8F0'),
        bottom=Side(style='thin', color='E2E8F0')
    )

    # 4. Write Title & Metadata Headers
    ws['A1'] = f"{business_name.upper()} — DAILY OPERATIONS MANIFEST"
    ws['A1'].font = title_font
    
    formatted_date = date.today().strftime("%A, %B %d, %Y")
    ws['A2'] = f"Generated automatically on {formatted_date} | Verzio Engine"
    ws['A2'].font = meta_font

    # 5. Write Table Column Headers
    headers = ["Time Slot", "Customer Name", "Phone Number", "Status"]
    for col_num, header_title in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_num)
        cell.value = header_title
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border
    
    ws.row_dimensions[4].height = 26

    # 6. Populate Rows from Database Ledger
    current_row = 5
    for appt in appointments:
        time_cell = ws.cell(row=current_row, column=1, value=appt.appointment_time.strftime("%I:%M %p"))
        name_cell = ws.cell(row=current_row, column=2, value=appt.customer_name if appt.customer_name else "Guest Client")
        phone_cell = ws.cell(row=current_row, column=3, value=appt.customer_phone)
        status_cell = ws.cell(row=current_row, column=4, value=appt.status.upper())
        
        # Center align everything except names
        time_cell.alignment = Alignment(horizontal="center")
        phone_cell.alignment = Alignment(horizontal="center")
        status_cell.alignment = Alignment(horizontal="center")
        name_cell.alignment = Alignment(horizontal="left")
        
        # Dynamic subtle coloring for status column
        if appt.status == "confirmed":
            status_cell.font = Font(name=font_family, size=11, bold=True, color="16A34A")
        elif appt.status == "cancelled" or appt.status == "no_show":
            status_cell.font = Font(name=font_family, size=11, bold=True, color="DC2626")
        else:
            status_cell.font = Font(name=font_family, size=11, bold=True, color="D97706")

        # Apply basic borders and universal data font to all row blocks
        for col_num in range(1, 5):
            c = ws.cell(row=current_row, column=col_num)
            if col_num != 4:  # status column keeps its custom color font
                c.font = data_font
            c.border = thin_border
            
        ws.row_dimensions[current_row].height = 20
        current_row += 1

    # Auto-adjust column widths cleanly based on contents so nothing gets cut off
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 4, 15)

    # 7. Write to static directory safely
    os.makedirs("exports", exist_ok=True)
    filename = f"exports/manifest_{business_id}_{date.today().isoformat()}.xlsx"
    wb.save(filename)
    
    return filename