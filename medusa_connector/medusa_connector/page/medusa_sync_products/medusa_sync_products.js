// Copyright (c) 2026, Aerele Technologies and contributors
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
		this.limit = 50;
		this.total = 0; // Total product count reported by the server
		this.allProducts = []; // Products of the currently loaded page
		this.syncRunning = false;
		this._filterTimer = null;
		this.init();
	}

	init() {
		frappe.run_serially([
			() => this.addMarkup(),
			() => this.bindActions(),
			() => this.fetchCounts(),
			() => this.fetchProducts(),
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
						display: flex;
						flex-direction: column;
						overflow: visible;
					}
					.medusa-sync-page-host {
					display: flex;
					flex-direction: column;
				}
				.medusa-sync-page {
					display: flex;
					margin-left: 0;
					margin-right: 0;
				}
				.medusa-sync-page > [class*="col-"] {
					display: flex;
					flex-direction: column;
					min-height: 0;
				}
				.medusa-sync-page .medusa-products-card {
					display: flex;
					flex-direction: column;
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
					overflow-x: auto;
				}
				.medusa-sync-page #medusa-product-list .datatable {
					width: 100%;
					max-width: 100%;
				}
				.medusa-sync-page #medusa-product-list .dt-header {
					flex: 0 0 auto;
				}
				.medusa-sync-page #medusa-product-list .dt-scrollable {
					overflow-x: auto !important;
					overflow-y: auto !important;
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
					display: none;
					flex-direction: column;
					height: 500px;
				}

				.medusa-sync-page #sync-log-card.is-visible {
					display: flex !important;
				}
				.medusa-sync-page #medusa-sync-log {
					flex: 1;
					overflow-y: auto;
					min-height: 0;
				}
				.medusa-sync-page .medusa-variant-toggle {
					display: inline-flex;
					align-items: center;
					gap: 4px;
					text-decoration: none;
					border: none;
					background: transparent;
				}
				.medusa-variant-popover {
					background: var(--card-bg, #fff);
					border: 1px solid var(--border-color, #d1d8dd);
					border-radius: 6px;
					box-shadow: var(--shadow-md, 0 4px 12px rgba(0,0,0,.15));
					min-width: 240px;
					max-width: 340px;
					max-height: 280px;
					overflow-y: auto;
					padding: 8px 0;
				}
				.medusa-variant-popover-header {
					font-weight: 600;
					font-size: 12px;
					padding: 4px 12px 8px;
					border-bottom: 1px solid var(--border-color, #eee);
					margin-bottom: 4px;
				}
				.medusa-variant-tree { padding: 0 4px; }
				.medusa-variant-node {
					position: relative;
					padding: 6px 8px 6px 22px;
					border-radius: 4px;
				}
				.medusa-variant-node::before {
					content: "";
					position: absolute;
					left: 8px;
					top: 14px;
					width: 8px;
					height: 1px;
					background: var(--border-color, #ccc);
			}
			.medusa-variant-node:hover { background: var(--bg-color, #f5f6f7); }
			.medusa-variant-node-title { font-size: 13px; font-weight: 500; }
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
	}
	buildVariantTree(product) {
		const variants = product.variants || [];
		if (!variants.length)
			return `<div class="text-muted small p-2">${__("No variants")}</div>`;
		return `
		<ul class="medusa-variant-tree list-unstyled mb-0">
			${variants
				.map(
					(v) => `
				<li class="medusa-variant-node">
					<div class="medusa-variant-node-title">${frappe.utils.escape_html(v.title || v.sku || v.id)}</div>
					<div class="text-muted small">
						${__("SKU")}: ${frappe.utils.escape_html(v.sku || "-")}
						${v.options ? ` · ${frappe.utils.escape_html(v.options)}` : ""}
					</div>
				</li>
			`
				)
				.join("")}
		</ul>
	`;
	}

	toggleVariantDropdown(e) {
		e.stopPropagation();
		const $btn = $(e.currentTarget);
		const productId = $btn.attr("data-id");

		if (this._activeDropdownId === productId) {
			this.closeVariantDropdown();
			return;
		}
		this.closeVariantDropdown();

		const product = this.allProducts.find((p) => p.id === productId);
		if (!product) return;

		const rect = $btn[0].getBoundingClientRect();
		const $pop = $(`
		<div class="medusa-variant-popover">
			<div class="medusa-variant-popover-header">${__("Variants")} (${
			(product.variants || []).length
		})</div>
			<div class="medusa-variant-popover-body">${this.buildVariantTree(product)}</div>
		</div>
	`);

		$("body").append($pop);
		const popWidth = $pop[0].offsetWidth;
		let left = rect.left + window.scrollX;
		if (left + popWidth > window.innerWidth - 10) left = window.innerWidth - popWidth - 10;

		$pop.css({
			position: "absolute",
			top: rect.bottom + window.scrollY + 4,
			left,
			zIndex: 1100,
		});

		this._activeDropdownId = productId;
		this._$activeDropdown = $pop;

		$(document).on("click.medusaVariantPopover", (ev) => {
			if (!$(ev.target).closest(".medusa-variant-popover, .medusa-variant-toggle").length) {
				this.closeVariantDropdown();
			}
		});
		$(window).on("resize.medusaVariantPopover scroll.medusaVariantPopover", () =>
			this.closeVariantDropdown()
		);
	}

	closeVariantDropdown() {
		if (this._$activeDropdown) {
			this._$activeDropdown.remove();
			this._$activeDropdown = null;
		}
		this._activeDropdownId = null;
		$(document).off("click.medusaVariantPopover");
		$(window).off("resize.medusaVariantPopover scroll.medusaVariantPopover");
	}

	bindActions() {
		// Typing filter triggers re-fetching server data once timer stops
		this.wrapper.on("click", ".medusa-variant-toggle", (e) => this.toggleVariantDropdown(e));
		this.wrapper.on("input", "#medusa-product-q", () => {
			clearTimeout(this._filterTimer);
			this._filterTimer = setTimeout(() => {
				this.offset = 0;
				this.fetchProducts();
			}, 350);
		});

		this.wrapper.on("keydown", "#medusa-product-q", (e) => {
			if (e.key === "Enter") {
				e.preventDefault();
				clearTimeout(this._filterTimer);
				this.offset = 0;
				this.fetchProducts();
			}
		});

		this.wrapper.on("change", "#medusa-product-status", () => {
			this.offset = 0;
			this.fetchProducts();
		});

		// Server-side pagination: each page fetches only its slice of the catalog
		this.wrapper.on("click", ".btn-prev", () => {
			if (this.offset <= 0) return;
			this.offset = Math.max(0, this.offset - this.limit);
			this.fetchProducts();
		});

		this.wrapper.on("click", ".btn-next", () => {
			if (this.offset + this.limit >= this.total) return;
			this.offset += this.limit;
			this.fetchProducts();
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
			console.error("Failed to fetch product counts", e);
		}
	}

	async fetchProducts() {
		const list = this.wrapper.find("#medusa-product-list");
		list.html(`<div class="text-center text-muted py-4">${__("Loading…")}</div>`);
		try {
			const q = this.wrapper.find("#medusa-product-q").val();
			const status = this.wrapper.find("#medusa-product-status").val();
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.get_products",
				args: { offset: this.offset, limit: this.limit, q, status },
			});

			this.allProducts = message.products || [];
			this.total = message.count || 0;
			this.renderTable();
		} catch (e) {
			list.html(
				`<div class="text-danger py-3">${__(
					"Could not load products. Check Medusa Settings connection."
				)}</div>`
			);
		}
	}

	renderTable() {
		this.closeVariantDropdown();
		const list = this.wrapper.find("#medusa-product-list");
		const total = this.total || this.allProducts.length;

		if (total === 0) {
			list.html(`<div class="text-center text-muted py-4">${__("No products found")}</div>`);
			this.wrapper.find(".medusa-datatable-footer").hide();
			return;
		}

		// The server already paginated; this.allProducts is the current page.
		const rows = this.allProducts.map((p, idx) => ({
			[__("S.No")]: this.offset + idx + 1,
			[__("Medusa Product ID")]: p.id,
			[__("Product Name")]: frappe.utils.escape_html(p.title || ""),
			[__("SKU")]:
				p.variants && p.variants.length > 1
					? `<button type="button" class="btn btn-link btn-xs medusa-variant-toggle" data-id="${
							p.id
					  }">
		 <span class="indicator-pill blue">${__("Template")}</span>
		 <i class="fa fa-angle-down"></i>
	   </button>`
					: frappe.utils.escape_html(p.sku || ""),
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

		// Reuse the existing table when possible; recreate only as a fallback.
		let rendered = false;
		if (this.table && typeof this.table.refresh === "function") {
			try {
				this.table.refresh(rows);
				rendered = true;
			} catch (e) {
				rendered = false;
			}
		}
		if (!rendered) {
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
		}

		// Update Pagination UI
		const startIdx = this.allProducts.length ? this.offset + 1 : 0;
		const endIdx = this.offset + this.allProducts.length;
		this.wrapper.find(".page-info").text(`${startIdx}-${endIdx} ${__("of")} ${total}`);

		this.wrapper.find(".medusa-datatable-footer").css({
			display: "flex",
			visibility: "visible",
		});
		this.wrapper.find(".btn-prev").prop("disabled", this.offset === 0);
		this.wrapper.find(".btn-next").prop("disabled", this.offset + this.limit >= total);
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
				this.markSynced(productId);
			} else {
				frappe.msgprint(message?.error || __("Sync failed"));
			}
		} catch (e) {
			frappe.msgprint(__("Sync failed"));
		} finally {
			$btn.prop("disabled", false).text(resync ? __("Re-sync") : __("Sync"));
		}
	}

	// Update the one synced row in place instead of re-fetching the catalog
	markSynced(productId) {
		const product = this.allProducts.find((p) => p.id === productId);
		if (product && !product.synced) {
			product.synced = true;
			const $count = this.wrapper.find("#count-synced");
			const n = parseInt($count.text(), 10);
			if (!isNaN(n)) $count.text(n + 1);
		}
		this.renderTable();
	}

	async startBulk() {
		if (this.syncRunning) {
			frappe.msgprint(__("Sync already in progress"));
			return;
		}
		const q = this.wrapper.find("#medusa-product-q").val();
		const status = this.wrapper.find("#medusa-product-status").val();

		this.logSync();

		try {
			const { message } = await frappe.call({
				method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.start_sync",
				args: { mode: "Full", q, status, force: 1 },
			});
			if (message?.status === "Busy") {
				frappe.msgprint(message.message);
				this.stopLogSync();
				return;
			}
			this.lastLogName = message?.log;
		} catch (e) {
			frappe.msgprint(__("Could not start sync"));
			this.stopLogSync();
		}
	}

	async checkSyncStatus() {
		const { message } = await frappe.call({
			method: "medusa_connector.medusa_connector.page.medusa_sync_products.medusa_sync_products.sync_status",
		});
		this.syncRunning = !!message?.running;
		if (this.syncRunning) {
			this.logSync();
		}
	}

	logSync() {
		const $card = this.wrapper.find("#sync-log-card");
		const $log = this.wrapper.find("#medusa-sync-log");
		$card.addClass("is-visible").css("display", "flex");
		$log.empty();

		this.toggleBulkButtons(true);

		frappe.realtime.off("medusa.key.sync.products");
		frappe.realtime.on("medusa.key.sync.products", (payload) => {
			const { message, done } = payload || {};
			if (message) {
				// Cap the log so long syncs do not grow the DOM without bound.
				const $entries = $log.children();
				if ($entries.length >= 200) $entries.first().remove();
				$log.append(`<pre class="mb-0">${message}</pre>`);
				requestAnimationFrame(() => {
					$log.scrollTop($log[0].scrollHeight);
				});
			}
			if (done) {
				frappe.realtime.off("medusa.key.sync.products");

				this.stopLogSync();
				this.fetchCounts();
				this.fetchProducts();
			}
		});
	}

	stopLogSync() {
		frappe.realtime.off("medusa.key.sync.products");
		this.toggleBulkButtons(false);
	}

	toggleBulkButtons(running) {
		this.syncRunning = running;
		this.wrapper.find("#btn-sync-all").prop("disabled", running);
		this.wrapper
			.find("#btn-sync-all")
			.text(running ? __("Syncing…") : __("Sync All Products"));
	}
}
