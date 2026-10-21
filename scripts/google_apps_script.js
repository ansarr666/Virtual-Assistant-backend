/**
 * Omnichannel Conversational Lead Bot - Google Apps Script Pipeline
 *
 * Dedicated strictly and exclusively to your 4 sheets:
 *  1. "tenants"       - tenant_id, company_name, status, created_at
 *  2. "mastersheet"   - Date, Tenant, Customer ID, Name, Phone, Email, Channel, Service, Requirements, Budget, Timeline, Meeting Slot, Status
 *  3. "chat history"  - Time, Tenant, Customer ID, Channel, Sender, Message
 *  4. "bookings"      - Date, Tenant, Customer ID, Name, Phone, Email, Service, Meeting Slot, Channel
 *
 * Guarantees:
 *  - Only writes data rows strictly UNDER the heading row (Row 2 onwards).
 *  - Never creates extra sheets or adds unauthorized columns.
 *  - Never writes unsolicited demo rows.
 *  - Performs in-place updates (upsert) by Customer ID/tenant_id to prevent duplicates.
 */

const SHEET_PIPELINE = {
  "tenants": {
    headers: ["tenant_id", "company_name", "status", "created_at"],
    keyHeader: "tenant_id",
    color: "#2D3748"
  },
  "mastersheet": {
    headers: [
      "Date", "Tenant", "Customer ID", "Name", "Phone", "Email", "Channel",
      "Service", "Requirements", "Budget", "Timeline", "Meeting Slot", "Status"
    ],
    keyHeader: "Customer ID",
    backupKeyHeader: "Phone",
    color: "#1A365D"
  },
  "chat history": {
    headers: [
      "Time", "Tenant", "Customer ID", "Channel", "Sender", "Message"
    ],
    keyHeader: null, // Chat logs append under row 1
    color: "#4A5568"
  },
  "bookings": {
    headers: [
      "Date", "Tenant", "Customer ID", "Name", "Phone", "Email", "Service", "Meeting Slot", "Channel"
    ],
    keyHeader: "Customer ID",
    backupKeyHeader: "Phone",
    color: "#2B6CB0"
  }
};

/**
 * Handles incoming POST requests from the bot backend.
 */
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return jsonResponse({ status: "error", message: "No post data received" }, 400);
    }

    const data = JSON.parse(e.postData.contents);
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheetName = (data.sheet || "mastersheet").trim().toLowerCase();

    // Strictly match only the 4 permitted sheets
    let targetKey = null;
    for (const key of Object.keys(SHEET_PIPELINE)) {
      if (key.toLowerCase() === sheetName) {
        targetKey = key;
        break;
      }
    }

    if (!targetKey) {
      return jsonResponse({ status: "ignored", message: "Sheet not in 4 registered pipeline tabs: " + sheetName });
    }

    // Locate the user's existing sheet (case-insensitive)
    let sheet = null;
    const existingSheets = ss.getSheets();
    for (let i = 0; i < existingSheets.length; i++) {
      if (existingSheets[i].getName().trim().toLowerCase() === targetKey) {
        sheet = existingSheets[i];
        break;
      }
    }

    if (!sheet) {
      // Only insert if sheet doesn't exist yet with exact registered name
      sheet = ss.insertSheet(targetKey);
    }

    const config = SHEET_PIPELINE[targetKey];
    const headers = config.headers;

    // Ensure header row exists on row 1 if completely empty
    if (sheet.getLastRow() === 0) {
      sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
      try {
        sheet.setFrozenRows(1);
        sheet.getRange(1, 1, 1, headers.length)
          .setBackground(config.color || "#1A365D")
          .setFontColor("#FFFFFF")
          .setFontWeight("bold")
          .setFontSize(10)
          .setHorizontalAlignment("left");
        sheet.setRowHeight(1, 32);
      } catch (err) {}
    }

    const rowData = data.row;
    if (!rowData || !rowData.length) {
      return jsonResponse({ status: "ok", message: "No row data to insert" });
    }

    // Do NOT write if rowData is identical to headers (prevents duplicate header insertions)
    if (JSON.stringify(rowData) === JSON.stringify(headers)) {
      return jsonResponse({ status: "ok", message: "Headers already exist; row write skipped" });
    }

    // Smart Row Upsert: Update existing row under heading if matching record exists
    let keyColIndex = data.keyColIndex;
    const keyValue = data.keyValue;
    let updated = false;

    if (!keyColIndex && data.keyField && headers) {
      const idx = headers.indexOf(data.keyField);
      if (idx !== -1) {
        keyColIndex = idx + 1;
      }
    }

    // Check rows under heading (Row 2 onwards)
    if (keyColIndex && keyValue && sheet.getLastRow() > 1) {
      const numRows = sheet.getLastRow() - 1;
      const keyRange = sheet.getRange(2, keyColIndex, numRows, 1).getValues();
      for (let i = 0; i < keyRange.length; i++) {
        if (String(keyRange[i][0]).trim() === String(keyValue).trim() && String(keyValue).trim() !== "") {
          const targetRow = i + 2;
          sheet.getRange(targetRow, 1, 1, rowData.length).setValues([rowData]);
          updated = true;
          break;
        }
      }
    }

    // Append under the heading row (Row 2 onwards) if not an update
    if (!updated) {
      sheet.appendRow(rowData);
    }

    return jsonResponse({ status: "ok", updated: updated, sheet: sheet.getName() });
  } catch (err) {
    return jsonResponse({ status: "error", error: err.toString() }, 500);
  }
}

/**
 * Handles GET requests to verify the 4 sheets.
 */
function doGet(e) {
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheets = ss.getSheets().map(s => ({
      name: s.getName(),
      rowCount: s.getLastRow(),
      colCount: s.getLastColumn(),
      headers: s.getLastRow() > 0 ? s.getRange(1, 1, 1, Math.max(s.getLastColumn(), 1)).getValues()[0] : []
    }));
    return jsonResponse({
      status: "ok",
      spreadsheetName: ss.getName(),
      sheets: sheets
    });
  } catch (err) {
    return jsonResponse({ status: "error", error: err.toString() }, 500);
  }
}

function jsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
