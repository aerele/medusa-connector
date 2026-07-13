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
		this._filterTimer = null;
		this.init();
	}

	init() {
		frappe.run_serially([
			() => this.addMarkup(),
			() => this.bindActions(),
			() => this.fetchCounts(),
			() => this.refreshTable(),
			() => this.checkSyncStatus(),
		]);
	}

	addMarkup() {
		this.wrapper.append(`
			<style>
				.medusa-sync-page .medusa-toolbar {
					display: flex;
					align-items: center;
					gap: 8px;
					flex-wrap: wrap;
				}
				.medusa-sync-page .medusa-toolbar .form-control {
					height: 28px;
					min-height: 28px;
				}
				.medusa-sync-page .medusa-action-cell {
					display: flex;
					align-items: center;
					justify-content: center;
					width: 100%;
					min-height: 28px;
				}
				.medusa-sync-page .medusa-action-cell .btn {
					min-width: 72px;
					margin: 0;
					line-height: 1.2;
				}
				.medusa-sync-page .dt-cell__content {
					align-items: center;
				}
				.medusa-sync-page .product-count > div {
					display: flex;
					flex-direction: column;
					align-items: center;
					justify-content: center;
				}
				.medusa-sync-page #btn-sync-all {
					display: flex;
					align-items: center;
					justify-content: center;
				}
			</style>
			<div class="row medusa-sync-page">
				<div class="col-lg-8 d-flex align-items-stretch">
					<div class="card border-0 shadow-sm p-3 mb-3 w-100 rounded-sm" style="background-color: var(--card-bg)">
						<div class="d-flex justify-content-between align-items-center border-bottom pb-2 mb-2 flex-wrap" style="gap: 8px;">
							<h5 class="mb-0">${__("Products in Medusa")}</h5>
							<div class="medusa-toolbar">
								<input type="text" class="form-control form-control-sm" id="medusa-product-q"
									placeholder="${__("Search product name…")}" style="width: 180px;"
									autocomplete="off">
								<select class="form-control form-control-sm" id="medusa-product-status" style="width: 140px;">
									<option value="">${__("All statuses")}</option>
									<option value="published">${__("Published")}</option>
									<option value="draft">${__("Draft")}</option>
									<option value="proposed">${__("Proposed")}</option>
									<option value="rejected">${__("Rejected")}</option>
								</select>
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
						<h5 class="border-bottom pb-2 mb-3">${__("Synchronization")}</h5>
						<button type="button" id="btn-sync-all" class="btn btn-primary w-100 font-weight-bold py-2 mb-3">
							${__("Sync All Products")}
						</button>
						<div class="product-count d-flex justify-content-stretch mb-0">
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
		// Live search: debounce typing so filter applies automatically.
		this.wrapper.on("input", "#medusa-product-q", () => {
			clearTimeout(this._filterTimer);
			this._filterTimer = setTimeout(() => {
				this.offset = 0;
				this.refreshTable();
			}, 350);
		});
		// Enter still applies immediately.
		this.wrapper.on("keydown", "#medusa-product-q", (e) => {
			if (e.key === "Enter") {
				e.preventDefault();
				clearTimeout(this._filterTimer);
				this.offset = 0;
				this.refreshTable();
			}
		});
		// Status changes apply filter immediately.
		this.wrapper.on("change", "#medusa-product-status", () => {
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
		this.wrapper.on("click", "#btn-sync-all", () => this.startBulk());
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
			/* ignore — counts optional when Medusa is unreachable */
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
			// Continuous S.No across pages: page 2 starts at offset+1 (e.g. 21, 22…).
			const base = cint(this.offset) || 0;
			const products = message.products || [];
			const rows = products.map((p, idx) => ({
				[__("S.No")]: base + idx + 1,
				[__("Medusa Product ID")]: p.id,
				[__("Product Name")]: frappe.utils.escape_html(p.title || ""),
				[__("SKU")]: frappe.utils.escape_html(p.sku || ""),
				[__("Medusa Status")]: this.statusPill(p.status),
				[__("ERPNext Sync Status")]: p.synced
					? `<span class="indicator-pill green">${__("Synced")}</span>`
					: `<span class="indicator-pill orange">${__("Not Synced")}</span>`,
				[__("Actions")]: p.synced
					? `<div class="medusa-action-cell"><button type="button" class="btn btn-default btn-xs btn-resync-one" data-id="${
							p.id
					  }">${__("Re-sync")}</button></div>`
					: `<div class="medusa-action-cell"><button type="button" class="btn btn-default btn-xs btn-sync-one" data-id="${
							p.id
					  }">${__("Sync")}</button></div>`,
			}));
			const columns = [
				{
					name: __("S.No"),
					editable: false,
					focusable: false,
					align: "center",
					width: 60,
				},
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
					width: 100,
				},
			];
			list.empty();
			this.table = new frappe.DataTable(list[0], {
				columns,
				data: rows,
				layout: "fluid",
				serialNoColumn: false,
			});
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
				frappe.show_alert({
					message: __("{0}: {1}", [message.action, message.item_code]),
					indicator: "green",
				});
				this.fetchCounts();
				this.refreshTable();
			} else {
				frappe.msgprint(message?.error || __("Sync failed"));
			}
		} catch (e) {
			frappe.msgprint(__("Sync failed"));
		} finally {
			$btn.prop("disabled", false).text(resync ? __("Re-sync") : __("Sync"));
		}
	}

	async startBulk() {
		if (this.syncRunning) {
			frappe.msgprint(__("Sync already in progress"));
			return;
		}
		const q = this.wrapper.find("#medusa-product-q").val();
		const status = this.wrapper.find("#medusa-product-status").val();
		try {
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.start_sync",
				args: { mode: "Full", q, status, force: 1 },
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
			const { message, synced, done } = payload || {};
			if (message) {
				$log.append(`<pre class="mb-0">${message}</pre>`);
				$log.scrollTop($log[0].scrollHeight);
			}
			if (synced) {
				const el = this.wrapper.find("#count-synced");
				const n = parseInt(el.text(), 10);
				if (!isNaN(n)) el.text(n + 1);
			}
			if (done) {
				frappe.realtime.off("medusa.key.sync.products");
				this.toggleBulkButtons(false);
				this.syncRunning = false;
				this.fetchCounts();
				this.refreshTable();
			}
		});
	}

	toggleBulkButtons(running) {
		this.syncRunning = running;
		this.wrapper.find("#btn-sync-all").prop("disabled", running);
		this.wrapper
			.find("#btn-sync-all")
			.text(running ? __("Syncing…") : __("Sync All Products"));
	}
}
