app_name = "intercompany_sync"
app_title = "Intercompany Sync"
app_publisher = "Rotiri Project"
app_description = "Intercompany transaction automation for JURI and outlets."
app_email = "rotiropi@users.noreply.github.com"
app_license = "mit"

required_apps = ["erpnext"]

fixtures = [
	{
		"dt": "Custom Field",
		"filters": [["name", "in", ["Payment Entry-ic_payment_reference"]]],
	}
]

doc_events = {
	"Purchase Receipt": {
		"validate": "intercompany_sync.guards.validate_purchase_receipt",
	},
	"Delivery Note": {
		"before_cancel": "intercompany_sync.guards.validate_delivery_note_cancel",
	},
	"Sales Invoice": {
		"validate": "intercompany_sync.guards.validate_sales_invoice",
		"on_submit": "intercompany_sync.jobs.on_sales_invoice_submit",
		"before_cancel": "intercompany_sync.cascade.before_cancel_sales_invoice",
		"on_cancel": "intercompany_sync.cascade.on_cancel_sales_invoice",
	},
	"Purchase Invoice": {
		"before_cancel": "intercompany_sync.guards.validate_purchase_invoice_cancel",
		"on_cancel": "intercompany_sync.cascade.on_cancel_purchase_invoice",
	},
	"Payment Entry": {
		"on_submit": "intercompany_sync.jobs.on_payment_entry_submit",
		"before_cancel": "intercompany_sync.cascade.before_cancel_payment_entry",
		"on_cancel": "intercompany_sync.cascade.on_cancel_payment_entry",
	},
}
