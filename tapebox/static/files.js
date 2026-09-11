/*
 * TapeBox Files Restore Planner
 *
 * Selection and planning only.
 * This file does not start tape operations.
 */
(() => {
    const selectAllButton =
        document.getElementById(
            "files-select-all-button"
        );

    const clearAllButton =
        document.getElementById(
            "files-clear-all-button"
        );

    const restoreSelectedButton =
        document.getElementById(
            "files-restore-selected-button"
        );

    const summary =
        document.getElementById(
            "files-selection-summary"
        );

    const planPanel =
        document.getElementById(
            "files-restore-plan"
        );

    if (
        !selectAllButton
        || !clearAllButton
        || !restoreSelectedButton
        || !summary
        || !planPanel
    ) {
        return;
    }


    function checkboxes() {
        return Array.from(
            document.querySelectorAll(
                ".files-select-checkbox"
            )
        );
    }


    function selectedCheckboxes() {
        return checkboxes().filter(
            checkbox => checkbox.checked
        );
    }


    function selectedFileIds() {
        return selectedCheckboxes()
            .map(
                checkbox =>
                    Number(
                        checkbox.dataset.fileId
                    )
            )
            .filter(
                fileId =>
                    Number.isInteger(fileId)
                    && fileId > 0
            );
    }


    function formatBytes(bytes) {
        let value =
            Number(bytes || 0);

        const units = [
            "B",
            "KB",
            "MB",
            "GB",
            "TB",
            "PB"
        ];

        let unit = 0;

        while (
            value >= 1000
            && unit < units.length - 1
        ) {
            value /= 1000;
            unit++;
        }

        return (
            value.toFixed(
                unit === 0 ? 0 : 2
            )
            + " "
            + units[unit]
        );
    }


    function updateSelection() {
        const selected =
            selectedCheckboxes();

        const count =
            selected.length;

        const totalBytes =
            selected.reduce(
                (total, checkbox) => {
                    return (
                        total
                        + Number(
                            checkbox.dataset.size
                            || 0
                        )
                    );
                },
                0
            );

        summary.textContent =
            count
            + (
                count === 1
                ? " file selected"
                : " files selected"
            )
            + " · "
            + formatBytes(totalBytes);

        clearAllButton.disabled =
            count === 0;

        restoreSelectedButton.disabled =
            count === 0;

        selectAllButton.disabled =
            checkboxes().length === 0;

        /*
         * Any selection change invalidates the
         * previously displayed restore plan.
         */
        planPanel.style.display =
            "none";

        planPanel.innerHTML = "";
    }


    function escapeHtml(value) {
        return String(
            value ?? ""
        )
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }


    function renderPlan(plan) {
        const tapes =
            Array.isArray(
                plan.required_tapes
            )
            ? plan.required_tapes
            : [];

        const tapeHtml =
            tapes.length
            ? tapes.map(
                (tape, index) => `
                    <div
                        style="
                            margin-top: 6px;
                        "
                    >
                        ${index + 1}.
                        <span class="tape-badge">
                            ${escapeHtml(tape.label)}
                        </span>
                    </div>
                `
            ).join("")
            : `
                <div class="muted">
                    No tapes found
                </div>
            `;

        planPanel.innerHTML = `
            <div
                style="
                    font-size: 17px;
                    font-weight: 700;
                    margin-bottom: 12px;
                "
            >
                Restore Plan
            </div>

            <div
                style="
                    line-height: 1.8;
                "
            >
                <div>
                    <strong>Files:</strong>
                    ${plan.file_count}
                </div>

                <div>
                    <strong>Total size:</strong>
                    ${formatBytes(plan.total_bytes)}
                </div>

                <div>
                    <strong>Required tapes:</strong>
                    ${tapes.length}
                </div>

                <div
                    style="
                        margin-left: 16px;
                        margin-bottom: 8px;
                    "
                >
                    ${tapeHtml}
                </div>

                <div>
                    <strong>Destination:</strong>
                    Restored Files
                </div>
            </div>
        `;

        planPanel.style.display =
            "block";
    }


    async function calculateRestorePlan() {
        const fileIds =
            selectedFileIds();

        if (!fileIds.length) {
            return;
        }

        restoreSelectedButton.disabled =
            true;

        restoreSelectedButton.textContent =
            "Planning...";

        planPanel.style.display =
            "block";

        planPanel.innerHTML =
            "Calculating restore requirements...";

        try {
            const response =
                await fetch(
                    "/api/files/restore-plan",
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json"
                        },
                        body: JSON.stringify({
                            file_ids: fileIds
                        })
                    }
                );

            const data =
                await response.json();

            if (
                !response.ok
                || !data.success
            ) {
                throw new Error(
                    data.error
                    || "Restore planning failed."
                );
            }

            renderPlan(data);

        } catch (error) {
            planPanel.innerHTML = `
                <div
                    style="
                        font-weight: 700;
                        margin-bottom: 8px;
                    "
                >
                    Restore Plan Error
                </div>

                <div>
                    ${escapeHtml(
                        error.message
                        || String(error)
                    )}
                </div>
            `;

            planPanel.style.display =
                "block";

        } finally {
            restoreSelectedButton.textContent =
                "Restore Selected";

            restoreSelectedButton.disabled =
                selectedFileIds().length === 0;
        }
    }


    document.addEventListener(
        "change",
        event => {
            if (
                !event.target.matches(
                    ".files-select-checkbox"
                )
            ) {
                return;
            }

            updateSelection();
        }
    );


    selectAllButton.addEventListener(
        "click",
        () => {
            for (
                const checkbox
                of checkboxes()
            ) {
                checkbox.checked = true;
            }

            updateSelection();
        }
    );


    clearAllButton.addEventListener(
        "click",
        () => {
            for (
                const checkbox
                of checkboxes()
            ) {
                checkbox.checked = false;
            }

            updateSelection();
        }
    );


    restoreSelectedButton.addEventListener(
        "click",
        calculateRestorePlan
    );


    updateSelection();
})();
