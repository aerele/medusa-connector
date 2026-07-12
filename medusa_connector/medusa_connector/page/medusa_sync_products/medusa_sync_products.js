// Copyright (c) 2026, Aerele and contributors
// For license information, please see license.txt

frappe.pages["medusa-sync-products"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Sync Medusa Products"),
		single_column: true,
	});
	new ProductSyncPage(wrapper, page);
};

class ProductSyncPage {
	constructor(wrapper, page) {
		this.wrapper = $(wrapper).find(".layout-main-section");
		this.page = page;
		this.offset = 0;
		this.limit = 20;
		this.syncRunning = false;
		this.summary = { created: 0, updated: 0, skipped: 0, failed: 0 };
		this.init();
	}

	init() {
		frappe.run_serially([
			() => this.addMarkup(),
			() => this.bindActions(),
			() => this.fetchCounts(),
			() => this.refreshTable(),
			() => this.loadHealth(),
			() => this.checkSyncStatus(),
		]);
	}

	addMarkup() {
		this.wrapper.append(`
			<div class="row">
				<div class="col-lg-8 d-flex align-items-stretch">
					<div class="card border-0 shadow-sm p-3 mb-3 w-100 rounded-sm" style="background-color: var(--card-bg)">
						<div class="d-flex justify-content-between align-items-center border-bottom pb-2 mb-2">
							<h5 class="mb-0">${__("Products in Medusa")}</h5>
							<div class="d-flex" style="gap: 8px;">
								<input type="text" class="form-control form-control-sm" id="medusa-product-q"
									placeholder="${__("Search…")}" style="width: 160px;">
								<select class="form-control form-control-sm" id="medusa-product-status" style="width: 130px;">
									<option value="">${__("All statuses")}</option>
									<option value="published">${__("Published")}</option>
									<option value="draft">${__("Draft")}</option>
									<option value="proposed">${__("Proposed")}</option>
									<option value="rejected">${__("Rejected")}</option>
								</select>
								<button type="button" class="btn btn-sm btn-default" id="btn-filter-products">${__(
									"Filter"
								)}</button>
							</div>
						</div>
						<div id="medusa-product-list"><div class="text-center text-muted py-4">${__(
							"Loading…"
						)}</div></div>
						<div class="medusa-datatable-footer mt-2 pt-3 pb-2 border-top text-right" style="display:none">
							<div class="btn-group">
								<button type="button" class="btn btn-sm btn-default btn-paginate btn-prev">${__("Prev")}</button>
								<button type="button" class="btn btn-sm btn-default btn-paginate btn-next">${__("Next")}</button>
							</div>
						</div>
					</div>
				</div>
				<div class="col-lg-4">
					<div class="card border-0 shadow-sm p-3 mb-3 rounded-sm" style="background-color: var(--card-bg)">
						<h5 class="border-bottom pb-2">${__("Synchronization")}</h5>
						<button type="button" id="btn-sync-full" class="btn btn-primary w-100 font-weight-bold py-2 mb-2">
							${__("Full Sync")}
						</button>
						<button type="button" id="btn-sync-incremental" class="btn btn-default w-100 py-2 mb-3">
							${__("Incremental Sync")}
						</button>
						<div class="product-count d-flex justify-content-stretch mb-2">
							<div class="text-center p-2 mx-1 rounded w-100" style="background-color: var(--bg-color)">
								<h3 id="count-medusa" class="mb-0">-</h3>
								<p class="text-muted m-0 small">${__("Medusa")}</p>
							</div>
							<div class="text-center p-2 mx-1 rounded w-100" style="background-color: var(--bg-color)">
								<h3 id="count-erpnext" class="mb-0">-</h3>
								<p class="text-muted m-0 small">${__("ERPNext")}</p>
							</div>
							<div class="text-center p-2 mx-1 rounded w-100" style="background-color: var(--bg-color)">
								<h3 id="count-synced" class="mb-0">-</h3>
								<p class="text-muted m-0 small">${__("Synced")}</p>
							</div>
						</div>
						<div class="border-top pt-2">
							<div class="small text-muted mb-1">${__("Import summary (this session)")}</div>
							<div class="d-flex flex-wrap" style="gap: 6px;">
								<span class="indicator-pill green" id="sum-created">0 ${__("Created")}</span>
								<span class="indicator-pill blue" id="sum-updated">0 ${__("Updated")}</span>
								<span class="indicator-pill orange" id="sum-skipped">0 ${__("Skipped")}</span>
								<span class="indicator-pill red" id="sum-failed">0 ${__("Failed")}</span>
							</div>
						</div>
						<div class="mt-3 d-flex flex-column" style="gap: 6px;">
							<button type="button" class="btn btn-xs btn-default" id="btn-open-mappings">
								${__("Open Item Mappings")}
							</button>
							<button type="button" class="btn btn-xs btn-default" id="btn-open-sync-logs">
								${__("Open Sync Logs")}
							</button>
							<button type="button" class="btn btn-xs btn-default" id="btn-refresh-health">
								${__("Refresh Health")}
							</button>
						</div>
					</div>

					<div class="card border-0 shadow-sm p-3 mb-3 rounded-sm" style="background-color: var(--card-bg)">
						<h5 class="border-bottom pb-2">${__("Health")}</h5>
						<div id="medusa-health" class="small text-muted">${__("Checking…")}</div>
					</div>

					<div class="card border-0 shadow-sm p-3 mb-3 rounded-sm" style="background-color: var(--card-bg); display:none;" id="sync-log-card">
						<h5 class="border-bottom pb-2">${__("Sync Progress")}</h5>
						<div class="progress mb-2" style="height: 8px;">
							<div class="progress-bar progress-bar-striped progress-bar-animated" id="sync-progress-bar"
								role="progressbar" style="width: 100%"></div>
						</div>
						<div class="control-value like-disabled-input for-description overflow-auto"
							id="medusa-sync-log" style="max-height: 360px;"></div>
					</div>
				</div>
			</div>
		`);
	}

	bindActions() {
		this.wrapper.on("click", "#btn-filter-products", () => {
			this.offset = 0;
			this.refreshTable();
		});
		this.wrapper.on("click", ".btn-prev", () => {
			if (this.prevOffset == null) return;
			this.offset = this.prevOffset;
			this.refreshTable();
		});
		this.wrapper.on("click", ".btn-next", () => {
			if (this.nextOffset == null) return;
			this.offset = this.nextOffset;
			this.refreshTable();
		});
		this.wrapper.on("click", ".btn-sync-one", (e) => this.syncOne($(e.currentTarget)));
		this.wrapper.on("click", ".btn-resync-one", (e) => this.syncOne($(e.currentTarget), true));
		this.wrapper.on("click", "#btn-sync-full", () => this.startBulk("Full"));
		this.wrapper.on("click", "#btn-sync-incremental", () => this.startBulk("Incremental"));
		this.wrapper.on("click", "#btn-open-mappings", () =>
			frappe.set_route("List", "Medusa Item Mapping")
		);
		this.wrapper.on("click", "#btn-open-sync-logs", () =>
			frappe.set_route("List", "Medusa Sync Log")
		);
		this.wrapper.on("click", "#btn-refresh-health", () => this.loadHealth());
	}

	async fetchCounts() {
		try {
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.get_product_count",
			});
			this.wrapper.find("#count-medusa").text(message.medusaCount ?? "-");
			this.wrapper.find("#count-erpnext").text(message.erpnextCount ?? "-");
			this.wrapper.find("#count-synced").text(message.syncedCount ?? "-");
		} catch (e) {
			/* connection errors surface in health card */
		}
	}

	async refreshTable() {
		const list = this.wrapper.find("#medusa-product-list");
		list.html(`<div class="text-center text-muted py-4">${__("Loading…")}</div>`);
		try {
			const q = this.wrapper.find("#medusa-product-q").val();
			const status = this.wrapper.find("#medusa-product-status").val();
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.get_products",
				args: { offset: this.offset, limit: this.limit, q, status },
			});
			this.nextOffset = message.nextOffset;
			this.prevOffset = message.prevOffset;
			const rows = (message.products || []).map((p) => ({
				[__("Medusa Product ID")]: p.id,
				[__("Product Name")]: frappe.utils.escape_html(p.title || ""),
				[__("SKU")]: frappe.utils.escape_html(p.sku || ""),
				[__("Medusa Status")]: this.statusPill(p.status),
				[__("ERPNext Sync Status")]: p.synced
					? `<span class="indicator-pill green">${__("Synced")}</span>`
					: `<span class="indicator-pill orange">${__("Not Synced")}</span>`,
				[__("Actions")]: p.synced
					? `<button type="button" class="btn btn-default btn-xs btn-resync-one" data-id="${
							p.id
					  }">${__("Re-sync")}</button>`
					: `<button type="button" class="btn btn-default btn-xs btn-sync-one" data-id="${
							p.id
					  }">${__("Sync")}</button>`,
			}));
			if (!this.table) {
				this.table = new frappe.DataTable(list[0], {
					columns: [
						{ name: __("Medusa Product ID"), editable: false, focusable: false },
						{ name: __("Product Name"), editable: false, focusable: false },
						{ name: __("SKU"), editable: false, focusable: false },
						{
							name: __("Medusa Status"),
							editable: false,
							focusable: false,
							align: "center",
						},
						{
							name: __("ERPNext Sync Status"),
							editable: false,
							focusable: false,
							align: "center",
						},
						{
							name: __("Actions"),
							editable: false,
							focusable: false,
							align: "center",
						},
					],
					data: rows,
					layout: "fluid",
				});
			} else {
				list.empty();
				this.table = new frappe.DataTable(list[0], {
					columns: this.table.options.columns,
					data: rows,
					layout: "fluid",
				});
			}
			this.wrapper.find(".medusa-datatable-footer").show();
			this.wrapper.find(".btn-prev").prop("disabled", this.prevOffset == null);
			this.wrapper.find(".btn-next").prop("disabled", this.nextOffset == null);
		} catch (e) {
			list.html(
				`<div class="text-danger py-3">${__(
					"Could not load products. Check Medusa Settings connection."
				)}</div>`
			);
		}
	}

	statusPill(status) {
		const s = (status || "").toLowerCase();
		const color = s === "published" ? "green" : s === "draft" ? "orange" : "gray";
		return `<span class="indicator-pill ${color}">${frappe.utils.escape_html(
			status || "-"
		)}</span>`;
	}

	async syncOne($btn, resync = false) {
		const productId = $btn.attr("data-id");
		$btn.prop("disabled", true).text(__("Syncing…"));
		try {
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.sync_product",
				args: { product_id: productId },
			});
			if (message && message.ok) {
				this.bumpSummary(message.action);
				frappe.show_alert({
					message: __("{0}: {1}", [message.action, message.item_code]),
					indicator: "green",
				});
				this.fetchCounts();
				this.refreshTable();
			} else {
				this.bumpSummary("failed");
				frappe.msgprint(message?.error || __("Sync failed"));
			}
		} catch (e) {
			this.bumpSummary("failed");
			frappe.msgprint(__("Sync failed"));
		} finally {
			$btn.prop("disabled", false).text(resync ? __("Re-sync") : __("Sync"));
		}
	}

	async startBulk(mode) {
		if (this.syncRunning) {
			frappe.msgprint(__("Sync already in progress"));
			return;
		}
		const q = this.wrapper.find("#medusa-product-q").val();
		const status = this.wrapper.find("#medusa-product-status").val();
		this.summary = { created: 0, updated: 0, skipped: 0, failed: 0 };
		this.renderSummary();
		try {
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.start_sync",
				args: { mode, q, status, force: 1 },
			});
			if (message?.status === "Busy") {
				frappe.msgprint(message.message);
				return;
			}
			this.lastLogName = message?.log;
			this.toggleBulkButtons(true);
			this.logSync();
		} catch (e) {
			frappe.msgprint(__("Could not start sync"));
		}
	}

	async checkSyncStatus() {
		const { message } = await frappe.call({
			method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.sync_status",
		});
		this.syncRunning = !!message?.running;
		if (this.syncRunning) {
			this.toggleBulkButtons(true);
			this.logSync();
		}
	}

	logSync() {
		const $card = this.wrapper.find("#sync-log-card");
		const $log = this.wrapper.find("#medusa-sync-log");
		$card.show();
		$log.html("");
		this.syncRunning = true;

		frappe.realtime.on("medusa.key.sync.products", (payload) => {
			const { message, synced, error, done } = payload || {};
			if (message) {
				$log.append(`<pre class="mb-0">${message}</pre>`);
				$log.scrollTop($log[0].scrollHeight);
			}
			if (synced) {
				// Live counter: treat synced events as updates in bulk path
				this.summary.updated += 1;
				this.renderSummary();
				const el = this.wrapper.find("#count-synced");
				const n = parseInt(el.text(), 10);
				if (!isNaN(n)) el.text(n + 1);
			}
			if (error) {
				this.summary.failed += 1;
				this.renderSummary();
			}
			if (done) {
				frappe.realtime.off("medusa.key.sync.products");
				this.toggleBulkButtons(false);
				this.syncRunning = false;
				this.fetchCounts();
				this.refreshTable();
				this.loadHealth();
			}
		});
	}

	toggleBulkButtons(running) {
		this.syncRunning = running;
		this.wrapper.find("#btn-sync-full, #btn-sync-incremental").prop("disabled", running);
		this.wrapper.find("#btn-sync-full").text(running ? __("Syncing…") : __("Full Sync"));
	}

	bumpSummary(action) {
		if (action === "created") this.summary.created += 1;
		else if (action === "updated") this.summary.updated += 1;
		else if (action === "skipped") this.summary.skipped += 1;
		else this.summary.failed += 1;
		this.renderSummary();
	}

	renderSummary() {
		this.wrapper.find("#sum-created").text(`${this.summary.created} ${__("Created")}`);
		this.wrapper.find("#sum-updated").text(`${this.summary.updated} ${__("Updated")}`);
		this.wrapper.find("#sum-skipped").text(`${this.summary.skipped} ${__("Skipped")}`);
		this.wrapper.find("#sum-failed").text(`${this.summary.failed} ${__("Failed")}`);
	}

	async loadHealth() {
		const $el = this.wrapper.find("#medusa-health");
		try {
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.get_health",
			});
			const conn = message.connection || {};
			const map = message.mapping || {};
			$el.html(`
				<div class="mb-2">
					<strong>${__("Connection")}:</strong>
					<span class="indicator-pill ${conn.ok ? "green" : "red"}">${
				conn.ok ? __("OK") : __("Error")
			}</span>
					<div class="text-muted">${frappe.utils.escape_html(conn.message || "")}</div>
				</div>
				<div>
					<strong>${__("Mappings")}:</strong> ${map.total ?? 0}<br>
					<span class="text-muted">
						${__("Orphaned")}: ${map.orphaned_count ?? 0} ·
						${__("Missing Items")}: ${map.missing_item_count ?? 0} ·
						${__("Duplicates")}: ${map.duplicate_count ?? 0}
					</span>
				</div>
			`);
		} catch (e) {
			$el.html(`<span class="text-danger">${__("Health check failed")}</span>`);
		}
	}
}
