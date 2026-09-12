/*
 * TapeBox Files Restore
 *
 * Handles catalog selection, restore planning, restore start,
 * and operation-status polling.
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

    let plannedFileIds = [];
    let activeOperationId = null;
    let operationPollTimer = null;
    let driveReadinessTimer = null;
    let restoreCompleted = false;

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
        if (activeOperationId) {
            return;
        }

        restoreCompleted = false;

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

        plannedFileIds = [];
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

                <div
                    id="files-drive-readiness"
                    style="
                        margin-top: 10px;
                    "
                >
                    <strong>Drive status:</strong>
                    Checking...
                </div>
            </div>

            <div
                style="
                    margin-top: 16px;
                "
            >
                <button
                    type="button"
                    id="files-start-restore-button"
                    class="button"
                    disabled
                >
                    Checking Drive...
                </button>
            </div>

            <div
                id="files-restore-operation"
                style="
                    margin-top: 16px;
                    display: none;
                "
            ></div>
        `;

        planPanel.style.display =
            "block";

        const startButton =
            document.getElementById(
                "files-start-restore-button"
            );

        if (startButton) {
            startButton.addEventListener(
                "click",
                startSelectedRestore
            );
        }

        updateDriveReadiness();
    }


    async function updateDriveReadiness() {
        if (driveReadinessTimer) {
            clearTimeout(
                driveReadinessTimer
            );

            driveReadinessTimer = null;
        }

        /*
         * An active restore owns the button state. Do not let
         * background drive polling overwrite Restoring/Waiting/
         * Restore Complete states.
         */
        if (
            activeOperationId
            || restoreCompleted
        ) {
            return;
        }

        const statusElement =
            document.getElementById(
                "files-drive-readiness"
            );

        const startButton =
            document.getElementById(
                "files-start-restore-button"
            );

        if (
            !statusElement
            || !startButton
        ) {
            return;
        }

        try {
            const response =
                await fetch(
                    "/api/tape/status"
                );

            const data =
                await response.json();

            if (
                activeOperationId
                || restoreCompleted
            ) {
                return;
            }

            if (
                !response.ok
                || !data.success
            ) {
                throw new Error(
                    data.error
                    || "Unable to read tape drive status."
                );
            }

            const busy =
                Boolean(
                    data.busy
                );

            const ready =
                Boolean(
                    !busy
                    && data.detected
                    && data.available
                    && data.online
                    && !data.mounted
                );

            if (busy) {
                statusElement.innerHTML =
                    "<strong>Drive status:</strong> "
                    + "Tape drive is currently in use.";

                startButton.disabled = true;
                startButton.textContent =
                    "Drive In Use...";

            } else if (ready) {
                statusElement.innerHTML =
                    "<strong>Drive status:</strong> Drive ready.";

                startButton.disabled = false;
                startButton.textContent =
                    "Start Restore";

            } else if (!data.detected) {
                statusElement.innerHTML =
                    "<strong>Drive status:</strong> "
                    + "Tape drive not detected.";

                startButton.disabled = true;
                startButton.textContent =
                    "Waiting for Drive...";

            } else if (data.mounted) {
                statusElement.innerHTML =
                    "<strong>Drive status:</strong> "
                    + "Tape drive is currently in use.";

                startButton.disabled = true;
                startButton.textContent =
                    "Drive In Use...";

            } else {
                statusElement.innerHTML =
                    "<strong>Drive status:</strong> "
                    + "Waiting for cartridge to become ready...";

                startButton.disabled = true;
                startButton.textContent =
                    "Waiting for Tape...";
            }

        } catch (error) {
            statusElement.innerHTML =
                "<strong>Drive status:</strong> "
                + "Unable to read drive status.";

            startButton.disabled = true;
            startButton.textContent =
                "Drive Status Error";
        }

        if (!activeOperationId) {
            driveReadinessTimer =
                setTimeout(
                    updateDriveReadiness,
                    2000
                );
        }
    }


    function setSelectionLocked(locked) {
        for (
            const checkbox
            of checkboxes()
        ) {
            checkbox.disabled = locked;
        }

        selectAllButton.disabled =
            locked
            || checkboxes().length === 0;

        clearAllButton.disabled =
            locked
            || selectedCheckboxes().length === 0;

        restoreSelectedButton.disabled =
            locked
            || selectedCheckboxes().length === 0;
    }


    function renderOperation(operation) {
        const container =
            document.getElementById(
                "files-restore-operation"
            );

        if (!container) {
            return;
        }

        const startButton =
            document.getElementById(
                "files-start-restore-button"
            );

        if (startButton) {
            if (
                operation.status
                === "completed"
            ) {
                startButton.disabled = true;
                startButton.textContent =
                    "Completed";

            } else if (
                operation.status
                === "waiting_for_tape"
            ) {
                startButton.disabled = true;
                startButton.textContent =
                    "Waiting for Tape...";

            } else if (
                operation.status
                === "failed"
            ) {
                startButton.disabled = false;
                startButton.textContent =
                    "Start Restore";

            } else {
                startButton.disabled = true;
                startButton.textContent =
                    "Restoring...";
            }
        }

        const result =
            operation.result || {};

        const transfer =
            operation.transfer;

        let transferHtml = "";

        if (transfer) {
            const copied =
                Number(
                    transfer.bytes_written
                    ?? 0
                );

            const total =
                Number(
                    transfer.bytes_total
                    ?? 0
                );

            const percent =
                Number(
                    transfer.percent
                    ?? (
                        total > 0
                        ? copied / total * 100
                        : 0
                    )
                );

            const speed =
                Number(
                    transfer.speed_bps
                    ?? 0
                );

            const eta =
                transfer.eta_seconds;

            const filename =
                transfer.filename
                ?? "";

            const partNumber =
                transfer.part_number;

            const partsTotal =
                transfer.parts_total;

            const partHtml =
                partNumber
                && partsTotal
                ? `
                    <div>
                        <strong>Part:</strong>
                        ${partNumber} of ${partsTotal}
                    </div>
                `
                : "";

            const speedHtml =
                speed > 0
                ? `
                    <div>
                        <strong>Speed:</strong>
                        ${formatBytes(speed)}/s
                    </div>
                `
                : "";

            const etaHtml =
                eta !== null
                && eta !== undefined
                && Number.isFinite(
                    Number(eta)
                )
                ? `
                    <div>
                        <strong>ETA:</strong>
                        ${Math.max(
                            0,
                            Math.round(
                                Number(eta)
                            )
                        )} sec
                    </div>
                `
                : "";

            transferHtml = `
                <div
                    style="
                        margin-top: 10px;
                        line-height: 1.6;
                    "
                >
                    ${
                        filename
                        ? `
                            <div>
                                <strong>File:</strong>
                                ${escapeHtml(filename)}
                            </div>
                        `
                        : ""
                    }

                    ${partHtml}

                    <div>
                        <strong>Transfer:</strong>
                        ${formatBytes(copied)}
                        /
                        ${formatBytes(total)}
                        (${Math.min(
                            100,
                            Math.max(
                                0,
                                percent
                            )
                        ).toFixed(1)}%)
                    </div>

                    ${speedHtml}
                    ${etaHtml}
                </div>
            `;
        }

        let alreadyRestoredHtml = "";

        if (
            operation.status === "completed"
            && result.already_restored
        ) {
            const skipped =
                Number(
                    result.files_skipped
                    ?? 0
                );

            alreadyRestoredHtml = `
                <div
                    style="
                        margin-top: 12px;
                        padding: 12px;
                        border: 1px solid #2f6f4e;
                        line-height: 1.6;
                    "
                >
                    <div
                        style="
                            font-weight: 700;
                        "
                    >
                        Already Restored
                    </div>

                    <div>
                        The existing destination file
                        matches the TapeBox catalog.
                    </div>

                    <div>
                        No tape access was required.
                    </div>

                    ${
                        skipped > 0
                        ? `
                            <div>
                                <strong>
                                    Files already restored:
                                </strong>
                                ${skipped}
                            </div>
                        `
                        : ""
                    }
                </div>
            `;
        }

        let tapesHtml = "";

        const requiredTapes =
            Array.isArray(
                result.required_tapes
            )
            ? result.required_tapes
            : [];

        if (
            operation.status
            === "waiting_for_tape"
            && requiredTapes.length
        ) {
            tapesHtml = `
                <div
                    style="
                        margin-top: 10px;
                    "
                >
                    <strong>Next required tape:</strong>
                    ${escapeHtml(
                        requiredTapes[0].label
                        ?? requiredTapes[0].tape_label
                        ?? requiredTapes[0].ltfs_uuid
                        ?? requiredTapes[0]
                    )}
                </div>

                <div
                    style="
                        margin-top: 12px;
                    "
                >
                    <button
                        type="button"
                        id="files-continue-restore-button"
                        class="button"
                    >
                        Continue Restore
                    </button>
                </div>
            `;
        }

        const messages =
            Array.isArray(
                operation.messages
            )
            ? operation.messages
            : [];

        let activityMessages =
            messages;

        const continueIndex =
            messages.lastIndexOf(
                "Continuing restore..."
            );

        if (continueIndex >= 0) {
            activityMessages =
                messages.slice(
                    continueIndex
                );
        }

        const recentMessages =
            operation.status === "completed"
                ? ""
                : activityMessages
                    .slice(-8)
                    .map(
                        message => `
                            <div>
                                ${escapeHtml(message)}
                            </div>
                        `
                    )
                    .join("");

        container.innerHTML = `
            <div
                style="
                    font-size: 16px;
                    font-weight: 700;
                    margin-bottom: 8px;
                "
            >
                Restore Status
            </div>

            <div>
                <strong>Status:</strong>
                ${escapeHtml(operation.status)}
            </div>

            <div
                style="
                    margin-top: 6px;
                "
            >
                ${escapeHtml(
                    operation.message
                    || ""
                )}
            </div>

            ${alreadyRestoredHtml}
            ${transferHtml}
            ${tapesHtml}

            ${
                recentMessages
                ? `
                    <div
                        style="
                            margin-top: 12px;
                        "
                    >
                        <strong>Activity:</strong>
                        <div
                            class="muted"
                            style="
                                margin-top: 4px;
                                line-height: 1.5;
                            "
                        >
                            ${recentMessages}
                        </div>
                    </div>
                `
                : ""
            }
        `;

        container.style.display =
            "block";

        const continueButton =
            document.getElementById(
                "files-continue-restore-button"
            );

        if (continueButton) {
            continueButton.addEventListener(
                "click",
                continueSelectedRestore
            );
        }
    }


    async function continueSelectedRestore() {
        if (!activeOperationId) {
            return;
        }

        const button =
            document.getElementById(
                "files-continue-restore-button"
            );

        if (button) {
            button.disabled = true;
            button.textContent =
                "Continuing...";
        }

        try {
            const response =
                await fetch(
                    "/api/operations/"
                    + encodeURIComponent(
                        activeOperationId
                    )
                    + "/resume",
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json"
                        },
                        body: "{}"
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
                    || "Unable to continue restore."
                );
            }

            await pollOperation();

        } catch (error) {
            if (button) {
                button.disabled = false;
                button.textContent =
                    "Continue Restore";
            }

            const container =
                document.getElementById(
                    "files-restore-operation"
                );

            if (container) {
                container.innerHTML += `
                    <div
                        style="
                            margin-top: 10px;
                            font-weight: 700;
                        "
                    >
                        Continue Restore Error
                    </div>

                    <div>
                        ${escapeHtml(
                            error.message
                            || String(error)
                        )}
                    </div>
                `;
            }
        }
    }


    async function pollOperation() {
        if (!activeOperationId) {
            return;
        }

        try {
            const response =
                await fetch(
                    "/api/operations/"
                    + encodeURIComponent(
                        activeOperationId
                    )
                );

            const data =
                await response.json();

            if (
                !response.ok
                || !data.success
                || !data.operation
            ) {
                throw new Error(
                    data.error
                    || "Unable to read restore status."
                );
            }

            const operation =
                data.operation;

            renderOperation(
                operation
            );

            if (
                operation.status
                === "completed"
                || operation.status
                === "failed"
            ) {
                activeOperationId = null;

                if (operationPollTimer) {
                    clearTimeout(
                        operationPollTimer
                    );

                    operationPollTimer = null;
                }

                if (
                    operation.status
                    === "completed"
                ) {
                    restoreCompleted = true;

                    if (driveReadinessTimer) {
                        clearTimeout(
                            driveReadinessTimer
                        );

                        driveReadinessTimer = null;
                    }

                    for (
                        const checkbox
                        of checkboxes()
                    ) {
                        checkbox.checked = false;
                    }
                }

                setSelectionLocked(
                    false
                );

                if (
                    operation.status
                    === "completed"
                ) {
                    summary.textContent =
                        "0 files selected · 0 B";

                    clearAllButton.disabled =
                        true;

                    restoreSelectedButton.disabled =
                        true;

                    selectAllButton.disabled =
                        checkboxes().length === 0;

                    plannedFileIds = [];
                }

                const startButton =
                    document.getElementById(
                        "files-start-restore-button"
                    );

                if (startButton) {
                    if (
                        operation.status
                        === "completed"
                    ) {
                        startButton.disabled = true;
                        startButton.textContent =
                            "Completed";
                    } else {
                        startButton.disabled = true;
                        startButton.textContent =
                            "Checking Drive...";
                    }
                }

                if (
                    operation.status
                    === "failed"
                ) {
                    updateDriveReadiness();
                }

                return;
            }

            operationPollTimer =
                setTimeout(
                    pollOperation,
                    1000
                );

        } catch (error) {
            const container =
                document.getElementById(
                    "files-restore-operation"
                );

            if (container) {
                container.innerHTML = `
                    <div
                        style="
                            font-weight: 700;
                        "
                    >
                        Restore Status Error
                    </div>

                    <div
                        style="
                            margin-top: 6px;
                        "
                    >
                        ${escapeHtml(
                            error.message
                            || String(error)
                        )}
                    </div>
                `;

                container.style.display =
                    "block";
            }

            operationPollTimer =
                setTimeout(
                    pollOperation,
                    2000
                );
        }
    }


    async function startSelectedRestore() {
        if (activeOperationId) {
            return;
        }

        const fileIds =
            plannedFileIds.slice();

        if (!fileIds.length) {
            return;
        }

        const startButton =
            document.getElementById(
                "files-start-restore-button"
            );

        if (startButton) {
            startButton.disabled = true;
            startButton.textContent =
                "Starting...";
        }

        setSelectionLocked(
            true
        );

        try {
            const response =
                await fetch(
                    "/api/files/restore-start",
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
                    || "Unable to start restore."
                );
            }

            activeOperationId =
                data.operation_id;

            await pollOperation();

        } catch (error) {
            const container =
                document.getElementById(
                    "files-restore-operation"
                );

            if (container) {
                container.innerHTML = `
                    <div
                        style="
                            font-weight: 700;
                        "
                    >
                        Restore Start Error
                    </div>

                    <div
                        style="
                            margin-top: 6px;
                        "
                    >
                        ${escapeHtml(
                            error.message
                            || String(error)
                        )}
                    </div>
                `;

                container.style.display =
                    "block";
            }

            setSelectionLocked(
                false
            );

            if (startButton) {
                startButton.disabled = false;
                startButton.textContent =
                    "Start Restore";
            }
        }
    }


    async function recoverActiveRestore() {
        /*
         * Reconnect the Files page to an unfinished selected-file
         * restore after a browser refresh or TapeBox restart.
         *
         * Discovery and plan rebuilding are read-only. Tape activity
         * does not resume until the user clicks Continue Restore.
         */

        setSelectionLocked(
            true
        );

        try {
            const response =
                await fetch(
                    "/api/files/restore-active"
                );

            const data =
                await response.json();

            if (
                !response.ok
                || !data.success
            ) {
                throw new Error(
                    data.error
                    || "Unable to check for an active restore."
                );
            }

            const operation =
                data.operation;

            if (!operation) {
                setSelectionLocked(
                    false
                );

                updateSelection();

                return;
            }

            const fileIds =
                Array.isArray(
                    operation.file_ids
                )
                ? operation.file_ids
                    .map(
                        value =>
                            Number(value)
                    )
                    .filter(
                        value =>
                            Number.isInteger(value)
                            && value > 0
                    )
                : [];

            /*
             * Restore visible checkbox selection where possible.
             * A search/filter may mean some persisted files are not
             * currently visible, so the restore plan itself still uses
             * the complete persisted file ID list.
             */
            const selectedIdSet =
                new Set(fileIds);

            for (
                const checkbox
                of checkboxes()
            ) {
                checkbox.checked =
                    selectedIdSet.has(
                        Number(
                            checkbox.dataset.fileId
                        )
                    );
            }

            /*
             * Update the visible selection summary before marking the
             * operation active. updateSelection intentionally refuses
             * to alter UI state while an operation is active.
             */
            updateSelection();

            if (!fileIds.length) {
                throw new Error(
                    "Recovered restore has no file IDs."
                );
            }

            const planResponse =
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

            const planData =
                await planResponse.json();

            if (
                !planResponse.ok
                || !planData.success
            ) {
                throw new Error(
                    planData.error
                    || "Unable to rebuild restore plan."
                );
            }

            plannedFileIds =
                fileIds.slice();

            renderPlan(
                planData
            );

            activeOperationId =
                operation.id;

            setSelectionLocked(
                true
            );

            renderOperation(
                operation
            );

            await pollOperation();

        } catch (error) {
            activeOperationId =
                null;

            setSelectionLocked(
                false
            );

            updateSelection();

            console.error(
                "Restore recovery failed:",
                error
            );
        }
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

            plannedFileIds =
                fileIds.slice();

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


    recoverActiveRestore();
})();
