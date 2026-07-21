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
		this.allProducts = []; // Local cache for all fetched products
		this.syncRunning = false;
		this._filterTimer = null;
		this.init();
	}

	init() {
		frappe.run_serially([
			() => this.addMarkup(),
			() => this.bindActions(),
			() => this.fetchCounts(),
			() => this.loadAllProducts(),
			() => this.checkSyncStatus(),
		]);
	}

	addMarkup() {
		this.wrapper.addClass("medusa-sync-page-host");
		this.wrapper
			.closest(".layout-main-section-wrapper")
			.addClass("medusa-sync-page-host-wrap");

		this.wrapper.append(`
			<style>
				.medusa-sync-page-host-wrap {
					--medusa-sync-page-height: calc(
						100vh - var(--navbar-height, 48px) - var(--page-head-height, 48px) - 16px
					);
					display: flex;
					flex-direction: column;
					height: var(--medusa-sync-page-height);
					min-height: var(--medusa-sync-page-height);
					max-height: var(--medusa-sync-page-height);
					margin-bottom: 0 !important;
					padding-bottom: 0 !important;
					overflow: hidden;
				}
				.medusa-sync-page-host {
					display: flex;
					flex-direction: column;
					flex: 1 1 auto;
					min-height: 0;
					height: 100%;
					padding-bottom: 0 !important;
					margin-bottom: 0 !important;
					overflow: hidden;
				}
				.medusa-sync-page {
					display: flex;
					flex: 1 1 auto;
					min-height: 0;
					height: 100%;
					margin-left: 0;
					margin-right: 0;
					margin-bottom: 0;
					overflow: hidden;
				}
				.medusa-sync-page > [class*="col-"] {
					display: flex;
					flex-direction: column;
					min-height: 0;
				}
				.medusa-sync-page .medusa-products-card {
					display: flex;
					flex-direction: column;
					flex: 1 1 auto;
					min-height: 0;
					height: 100%;
					margin-bottom: 0 !important;
				}
				.medusa-sync-page .medusa-products-card-header,
				.medusa-sync-page .medusa-datatable-footer {
					flex: 0 0 auto;
				}
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
				.medusa-sync-page #medusa-product-list {
					width: 100%;
					max-width: 100%;
					flex: 1 1 auto;
					min-height: 200px;
					overflow: hidden;
					display: flex;
					flex-direction: column;
				}
				.medusa-sync-page #medusa-product-list .datatable {
					width: 100%;
					max-width: 100%;
					flex: 1 1 auto;
					min-height: 0;
					height: 100%;
					display: flex;
					flex-direction: column;
				}
				.medusa-sync-page #medusa-product-list .dt-header {
					flex: 0 0 auto;
				}
				.medusa-sync-page #medusa-product-list .dt-scrollable {
					flex: 1 1 auto;
					overflow-x: auto !important;
					overflow-y: auto !important;
					height: 100% !important;
					max-height: none !important;
					min-height: 160px;
				}
				.medusa-sync-page #medusa-product-list .dt-row {
					min-width: max-content;
				}
				.medusa-sync-page .dt-cell__content {
					align-items: center;
					white-space: nowrap;
					overflow: hidden;
					text-overflow: ellipsis;
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
				.medusa-sync-page .medusa-side-col {
					gap: 12px;
				}
				.medusa-sync-page #sync-log-card {
					flex: 1 1 auto;
					min-height: 0;
					display: none;
					flex-direction: column;
					margin-bottom: 0 !important;
				}
				.medusa-sync-page #sync-log-card.is-visible {
					display: flex !important;
				}
				.medusa-sync-page #medusa-sync-log {
					flex: 1 1 auto;
					min-height: 120px;
					max-height: none;
				}
			</style>
			<div class="row medusa-sync-page">
				<div class="col-lg-8 d-flex flex-column">
					<div class="card border-0 shadow-sm p-3 w-100 rounded-sm medusa-products-card" style="background-color: var(--card-bg)">
						<div class="d-flex justify-content-between align-items-center border-bottom pb-2 mb-2 flex-wrap medusa-products-card-header" style="gap: 8px;">
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
						<div class="medusa-datatable-footer mt-2 pt-3 pb-2 border-top d-flex justify-content-between align-items-center" style="display:none">
							<span class="text-muted small page-info"></span>
							<div class="btn-group">
								<button type="button" class="btn btn-sm btn-default btn-paginate btn-prev">${__("Prev")}</button>
								<button type="button" class="btn btn-sm btn-default btn-paginate btn-next">${__("Next")}</button>
							</div>
						</div>
					</div>
				</div>
				<div class="col-lg-4 d-flex flex-column medusa-side-col">
					<div class="card border-0 shadow-sm p-3 rounded-sm" style="background-color: var(--card-bg)">
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

					<div class="card border-0 shadow-sm p-3 rounded-sm" style="background-color: var(--card-bg);" id="sync-log-card">
						<h5 class="border-bottom pb-2">${__("Sync Progress")}</h5>
						<div class="progress mb-2" style="height: 8px;">
							<div class="progress-bar progress-bar-striped progress-bar-animated" id="sync-progress-bar"
								role="progressbar" style="width: 100%"></div>
						</div>
						<div class="control-value like-disabled-input for-description overflow-auto"
							id="medusa-sync-log"></div>
					</div>
				</div>
			</div>
		`);

		this.wrapper.find("#sync-log-card").hide();

		if (!this._boundResize) {
			this._boundResize = frappe.utils.debounce(() => this.fitTableHeight(), 100);
			$(window).on("resize.medusa-sync-products", this._boundResize);
		}
	}

	fitTableHeight() {
		const $list = this.wrapper.find("#medusa-product-list");
		const $scrollable = $list.find(".dt-scrollable");
		if (!$list.length || !$scrollable.length) return;

		const listEl = $list[0];
		const headerEl = $list.find(".dt-header")[0];
		const headerH = headerEl ? headerEl.offsetHeight : 0;
		let available = listEl.clientHeight - headerH;

		if (available < 160) {
			const card = this.wrapper.find(".medusa-products-card")[0];
			const cardHeader = this.wrapper.find(".medusa-products-card-header")[0];
			const footer = this.wrapper.find(".medusa-datatable-footer")[0];
			const cardPad = 24;
			const used =
				(cardHeader ? cardHeader.offsetHeight : 0) +
				(footer && footer.offsetParent ? footer.offsetHeight : 0) +
				headerH +
				cardPad +
				16;
			const hostTop = card ? card.getBoundingClientRect().top : 80;
			available = Math.max(160, window.innerHeight - hostTop - used);
		}

		const el = $scrollable[0];
		el.style.setProperty("height", `${available}px`, "important");
		el.style.setProperty("max-height", `${available}px`, "important");
		el.style.setProperty("overflow-y", "auto", "important");
		el.style.setProperty("overflow-x", "auto", "important");
	}

	bindActions() {
		// Typing filter triggers re-fetching server data once timer stops
		this.wrapper.on("input", "#medusa-product-q", () => {
			clearTimeout(this._filterTimer);
			this._filterTimer = setTimeout(() => {
				this.offset = 0;
				this.loadAllProducts();
			}, 350);
		});

		this.wrapper.on("keydown", "#medusa-product-q", (e) => {
			if (e.key === "Enter") {
				e.preventDefault();
				clearTimeout(this._filterTimer);
				this.offset = 0;
				this.loadAllProducts();
			}
		});

		this.wrapper.on("change", "#medusa-product-status", () => {
			this.offset = 0;
			this.loadAllProducts();
		});

		// Local Pagination: No network request
		this.wrapper.on("click", ".btn-prev", () => {
			if (this.offset <= 0) return;
			this.offset = Math.max(0, this.offset - this.limit);
			this.renderTable();
		});

		this.wrapper.on("click", ".btn-next", () => {
			if (this.offset + this.limit >= this.allProducts.length) return;
			this.offset += this.limit;
			this.renderTable();
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
			/* ignore when Medusa is unreachable */
		}
	}

	/**
	 * Single API Call: Fetch all products matching current query/status parameters
	 */
	async loadAllProducts() {
		const list = this.wrapper.find("#medusa-product-list");
		list.html(`<div class="text-center text-muted py-4">${__("Loading…")}</div>`);
		try {
			const q = this.wrapper.find("#medusa-product-q").val();
			const status = this.wrapper.find("#medusa-product-status").val();
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.get_products",
				args: { fetch_all: 1, q, status },
			});

			this.allProducts = message.products || [];
			this.offset = 0;
			this.renderTable();
		} catch (e) {
			list.html(
				`<div class="text-danger py-3">${__(
					"Could not load products. Check Medusa Settings connection."
				)}</div>`
			);
		}
	}

	/**
	 * Local Rendering: Slices cached `allProducts` array for current page
	 */
	renderTable() {
		const list = this.wrapper.find("#medusa-product-list");
		const total = this.allProducts.length;

		if (total === 0) {
			list.html(`<div class="text-center text-muted py-4">${__("No products found")}</div>`);
			this.wrapper.find(".medusa-datatable-footer").hide();
			return;
		}

		const pageSlice = this.allProducts.slice(this.offset, this.offset + this.limit);

		const rows = pageSlice.map((p, idx) => ({
			[__("S.No")]: this.offset + idx + 1,
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
			{ name: __("S.No"), editable: false, focusable: false, align: "center", width: 70 },
			{ name: __("Medusa Product ID"), editable: false, focusable: false, width: 300 },
			{ name: __("Product Name"), editable: false, focusable: false, width: 180 },
			{ name: __("SKU"), editable: false, focusable: false, width: 220 },
			{
				name: __("Medusa Status"),
				editable: false,
				focusable: false,
				align: "center",
				width: 140,
			},
			{
				name: __("ERPNext Sync Status"),
				editable: false,
				focusable: false,
				align: "center",
				width: 160,
			},
			{
				name: __("Actions"),
				editable: false,
				focusable: false,
				align: "center",
				width: 120,
			},
		];

		list.empty();
		if (this.table && typeof this.table.destroy === "function") {
			try {
				this.table.destroy();
			} catch (e) {
				/* ignore stale instance */
			}
			this.table = null;
		}

		this.table = new frappe.DataTable(list[0], {
			columns,
			data: rows,
			layout: "fixed",
			serialNoColumn: false,
			checkboxColumn: false,
			inlineFilters: false,
			noDataMessage: __("No products found"),
		});

		// Update Pagination UI
		const startIdx = this.offset + 1;
		const endIdx = Math.min(this.offset + this.limit, total);
		this.wrapper.find(".page-info").text(`${startIdx}-${endIdx} ${__("of")} ${total}`);

		this.wrapper.find(".medusa-datatable-footer").show();
		this.wrapper.find(".btn-prev").prop("disabled", this.offset === 0);
		this.wrapper.find(".btn-next").prop("disabled", this.offset + this.limit >= total);

		requestAnimationFrame(() => this.fitTableHeight());
		setTimeout(() => this.fitTableHeight(), 50);
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
				this.loadAllProducts();
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
		$card.addClass("is-visible").show();
		$log.html("");
		this.syncRunning = true;
		requestAnimationFrame(() => this.fitTableHeight());

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
				this.loadAllProducts();
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
