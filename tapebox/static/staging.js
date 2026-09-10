function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) {
        return "-";
    }

    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let index = 0;

    while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index++;
    }

    const digits =
        index === 0 ? 0 :
        value >= 100 ? 0 :
        value >= 10 ? 1 : 2;

    return `${value.toFixed(digits)} ${units[index]}`;
}


function formatSpeed(bytesPerSecond) {
    if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) {
        return "-";
    }

    return `${formatBytes(bytesPerSecond)}/s`;
}


function formatElapsed(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) {
        return "-";
    }

    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;

    if (hours > 0) {
        return [
            hours,
            String(minutes).padStart(2, "0"),
            String(secs).padStart(2, "0")
        ].join(":");
    }

    return [
        String(minutes).padStart(2, "0"),
        String(secs).padStart(2, "0")
    ].join(":");
}


function renderTransfer(detail, transfer) {
    if (!detail || !transfer) {
        return false;
    }

    const percent = Math.max(
        0,
        Math.min(
            Number(transfer.percent) || 0,
            100
        )
    );

    let phase = "Writing";

    if (transfer.phase === "finalizing") {
        phase = "Finalizing";
    } else if (
        transfer.phase === "finalizing_file"
    ) {
        phase = "Finalizing File";
    }

    const progress =
        document.getElementById(
            "archive-progress"
        );

    const progressBar =
        document.getElementById(
            "archive-progress-bar"
        );

    const progressPercent =
        document.getElementById(
            "archive-progress-percent"
        );

    if (progress) {
        progress.style.display = "block";
    }

    if (progressBar) {
        progressBar.style.width =
            `${percent}%`;
    }

    if (progressPercent) {
        progressPercent.textContent =
            `${percent.toFixed(1)}%`;
    }

    const lines = [
        `Phase: ${phase}`
    ];

    if (transfer.filename) {
        lines.push(
            `File: ${transfer.filename}`
        );
    }

    lines.push(
        `${transfer.kind === "folder" ? "File Progress" : "Progress"}: ${percent.toFixed(1)}%`
    );

    lines.push(
        `Transferred: ${formatBytes(
            Number(transfer.bytes_done) || 0
        )} / ${formatBytes(
            Number(transfer.bytes_total) || 0
        )}`
    );

    lines.push(
        `Speed: ${formatSpeed(
            Number(
                transfer.speed_bytes_per_second
            ) || 0
        )}`
    );

    lines.push(
        `Elapsed: ${formatElapsed(
            Number(
                transfer.elapsed_seconds
            ) || 0
        )}`
    );

    if (transfer.tape_label) {
        lines.push(
            `Tape: ${transfer.tape_label}`
        );
    }

    detail.textContent =
        lines.join("\n");

    const jobProgress =
        document.getElementById(
            "archive-job-progress"
        );

    const jobFiles =
        document.getElementById(
            "archive-job-files"
        );

    const jobBytes =
        document.getElementById(
            "archive-job-bytes"
        );

    const jobProgressBar =
        document.getElementById(
            "archive-job-progress-bar"
        );

    const jobProgressPercent =
        document.getElementById(
            "archive-job-progress-percent"
        );

    if (
        transfer.kind === "folder" &&
        Number.isFinite(
            Number(transfer.job_percent)
        )
    ) {
        const jobPercent = Math.max(
            0,
            Math.min(
                Number(
                    transfer.job_percent
                ) || 0,
                100
            )
        );

        if (jobProgress) {
            jobProgress.style.display =
                "block";
        }

        if (jobFiles) {
            jobFiles.textContent =
                `Files: ${
                    Number(
                        transfer.job_files_done
                    ) || 0
                } / ${
                    Number(
                        transfer.job_files_total
                    ) || 0
                }`;
        }

        if (jobBytes) {
            jobBytes.textContent =
                `Total: ${formatBytes(
                    Number(
                        transfer.job_bytes_done
                    ) || 0
                )} / ${formatBytes(
                    Number(
                        transfer.job_bytes_total
                    ) || 0
                )}`;
        }

        if (jobProgressBar) {
            jobProgressBar.style.width =
                `${jobPercent}%`;
        }

        if (jobProgressPercent) {
            jobProgressPercent.textContent =
                `${jobPercent.toFixed(1)}%`;
        }

    } else if (jobProgress) {
        jobProgress.style.display =
            "none";
    }

    return true;
}


document.addEventListener(
    "click",
    async function (event) {
        const button =
            event.target.closest(
                ".archive-button"
            );

        if (!button) {
            return;
        }

        event.preventDefault();
        event.stopPropagation();

        const path =
            button.dataset.path;

        if (!path) {
            alert(
                "TapeBox: Archive path is missing."
            );
            return;
        }

        if (
            !confirm(
                `Archive "${path}" to the currently loaded tape?`
            )
        ) {
            return;
        }

        const panel =
            document.getElementById(
                "archive-status-panel"
            );

        const message =
            document.getElementById(
                "archive-status-message"
            );

        const detail =
            document.getElementById(
                "archive-status-detail"
            );

        if (panel) {
            panel.style.display = "block";
        }

        if (message) {
            message.textContent =
                "Starting archive...";
        }

        if (detail) {
            detail.textContent =
                `Source: ${path}`;
        }

        document
            .querySelectorAll(
                ".archive-button"
            )
            .forEach((item) => {
                item.disabled = true;
            });

        try {
            const form =
                new FormData();

            form.append(
                "path",
                path
            );

            const response =
                await fetch(
                    "/api/staging/archive",
                    {
                        method: "POST",
                        body: form
                    }
                );

            const data =
                await response.json();

            if (
                !response.ok ||
                !data.success
            ) {
                throw new Error(
                    data.error ||
                    "Could not start archive."
                );
            }

            const operationId =
                data.operation_id;

            if (message) {
                message.textContent =
                    "Archive started...";
            }

            window.monitorTapeBoxArchiveOperation(
                operationId
            );

        } catch (error) {
            if (message) {
                message.textContent =
                    "Archive failed to start.";
            }

            if (detail) {
                detail.textContent =
                    error.message;
            }

            document
                .querySelectorAll(
                    ".archive-button"
                )
                .forEach((item) => {
                    item.disabled = false;
                });
        }
    }
);



//
// Shared live archive-operation monitor.
//
// Used by both:
//   - individual Archive
//   - Archive Selected
//
window.monitorTapeBoxArchiveOperation =
    async function (operationId) {
        const panel =
            document.getElementById(
                "archive-status-panel"
            );

        const message =
            document.getElementById(
                "archive-status-message"
            );

        const detail =
            document.getElementById(
                "archive-status-detail"
            );

        const selectedButton =
            document.getElementById(
                "staging-archive-selected-button"
            );

        if (panel) {
            panel.style.display = "block";
        }

        function enableArchiveControls() {
            document
                .querySelectorAll(
                    ".archive-button"
                )
                .forEach((item) => {
                    item.disabled = false;
                });

            if (selectedButton) {
                selectedButton.disabled = false;
                selectedButton.textContent =
                    "Archive Selected";
            }
        }

        async function poll() {
            try {
                const statusResponse =
                    await fetch(
                        `/api/operations/${operationId}`
                    );

                const statusData =
                    await statusResponse.json();

                if (
                    !statusResponse.ok ||
                    !statusData.success
                ) {
                    throw new Error(
                        statusData.error ||
                        "Could not read archive status."
                    );
                }

                const operation =
                    statusData.operation;

                if (message) {
                    message.textContent =
                        operation.message ||
                        operation.status ||
                        "Working...";
                }

                const hasTransfer =
                    renderTransfer(
                        detail,
                        operation.transfer
                    );

                if (
                    !hasTransfer &&
                    detail
                ) {
                    const lines = [
                        `Status: ${operation.status}`
                    ];

                    if (
                        operation.job_id
                    ) {
                        lines.push(
                            `Archive Job: ${operation.job_id}`
                        );
                    }

                    if (
                        operation.messages &&
                        operation.messages.length
                    ) {
                        lines.push("");
                        lines.push(
                            ...operation.messages.slice(-8)
                        );
                    }

                    detail.textContent =
                        lines.join("\n");
                }

                if (
                    operation.status ===
                        "completed" ||
                    operation.status ===
                        "failed" ||
                    operation.status ===
                        "waiting_for_tape"
                ) {
                    enableArchiveControls();
                    return;
                }

                setTimeout(
                    poll,
                    1000
                );

            } catch (error) {
                if (message) {
                    message.textContent =
                        "Archive status error.";
                }

                if (detail) {
                    detail.textContent =
                        error.message;
                }

                enableArchiveControls();
            }
        }

        poll();
    };


//
// Web upload into the current staging folder.
//
(function () {
    const zone =
        document.getElementById(
            "staging-upload-zone"
        );

    const browse =
        document.getElementById(
            "staging-upload-browse"
        );

    const input =
        document.getElementById(
            "staging-upload-input"
        );

    const status =
        document.getElementById(
            "staging-upload-status"
        );

    const message =
        document.getElementById(
            "staging-upload-message"
        );

    const progressBar =
        document.getElementById(
            "staging-upload-progress-bar"
        );

    const progressText =
        document.getElementById(
            "staging-upload-progress-text"
        );

    if (
        !zone ||
        !browse ||
        !input
    ) {
        return;
    }

    function uploadFiles(files) {
        const selected =
            Array.from(files || []);

        if (!selected.length) {
            return;
        }

        const form =
            new FormData();

        form.append(
            "path",
            window.TAPEBOX_STAGING_PATH || ""
        );

        selected.forEach((file) => {
            form.append(
                "files",
                file,
                file.name
            );
        });

        if (status) {
            status.style.display =
                "block";
        }

        if (message) {
            message.textContent =
                `Uploading ${selected.length} file${
                    selected.length === 1
                        ? ""
                        : "s"
                }...`;
        }

        if (progressBar) {
            progressBar.style.width =
                "0%";
        }

        if (progressText) {
            progressText.textContent =
                "0.0%";
        }

        browse.disabled = true;
        input.disabled = true;

        const xhr =
            new XMLHttpRequest();

        xhr.open(
            "POST",
            "/api/staging/upload"
        );

        xhr.upload.addEventListener(
            "progress",
            (event) => {
                if (!event.lengthComputable) {
                    return;
                }

                const percent =
                    Math.min(
                        100,
                        (
                            event.loaded
                            / event.total
                        ) * 100
                    );

                if (progressBar) {
                    progressBar.style.width =
                        `${percent}%`;
                }

                if (progressText) {
                    progressText.textContent =
                        `${percent.toFixed(1)}% — ${
                            formatBytes(
                                event.loaded
                            )
                        } / ${
                            formatBytes(
                                event.total
                            )
                        }`;
                }
            }
        );

        xhr.addEventListener(
            "load",
            () => {
                browse.disabled = false;
                input.disabled = false;

                let data = null;

                try {
                    data = JSON.parse(
                        xhr.responseText
                    );
                } catch (error) {
                    data = null;
                }

                if (
                    xhr.status >= 200 &&
                    xhr.status < 300 &&
                    data &&
                    data.success
                ) {
                    if (progressBar) {
                        progressBar.style.width =
                            "100%";
                    }

                    if (progressText) {
                        progressText.textContent =
                            "100.0%";
                    }

                    if (message) {
                        message.textContent =
                            `Upload complete — ${
                                data.count
                            } file${
                                data.count === 1
                                    ? ""
                                    : "s"
                            }.`;
                    }

                    window.setTimeout(
                        () => {
                            window.location.reload();
                        },
                        700
                    );

                    return;
                }

                if (message) {
                    message.textContent =
                        (
                            data &&
                            data.error
                        )
                            ? `Upload failed: ${data.error}`
                            : "Upload failed.";
                }
            }
        );

        xhr.addEventListener(
            "error",
            () => {
                browse.disabled = false;
                input.disabled = false;

                if (message) {
                    message.textContent =
                        "Upload failed: network error.";
                }
            }
        );

        xhr.send(form);
    }

    browse.addEventListener(
        "click",
        (event) => {
            event.stopPropagation();
            input.click();
        }
    );

    zone.addEventListener(
        "click",
        (event) => {
            if (
                event.target !== browse
            ) {
                input.click();
            }
        }
    );

    input.addEventListener(
        "change",
        () => {
            uploadFiles(
                input.files
            );
        }
    );

    zone.addEventListener(
        "dragover",
        (event) => {
            event.preventDefault();
        }
    );

    zone.addEventListener(
        "drop",
        (event) => {
            event.preventDefault();

            uploadFiles(
                event.dataTransfer.files
            );
        }
    );
})();


/*
 * TapeBox Staging Archive Planner
 */
(() => {
    const selectAllButton =
        document.getElementById(
            "staging-select-all-button"
        );

    const clearAllButton =
        document.getElementById(
            "staging-clear-all-button"
        );

    const archiveSelectedButton =
        document.getElementById(
            "staging-archive-selected-button"
        );

    const summary =
        document.getElementById(
            "staging-selection-summary"
        );

    const planPanel =
        document.getElementById(
            "staging-archive-plan"
        );

    const planStatus =
        document.getElementById(
            "staging-plan-status"
        );

    const planDetails =
        document.getElementById(
            "staging-plan-details"
        );

    if (
        !selectAllButton
        || !clearAllButton
        || !archiveSelectedButton
        || !summary
        || !planPanel
        || !planStatus
        || !planDetails
    ) {
        return;
    }


    let plannerTimer = null;
    let plannerRequest = 0;


    function checkboxes() {
        return Array.from(
            document.querySelectorAll(
                ".staging-select-checkbox"
            )
        );
    }


    function selectedPaths() {
        return checkboxes()
            .filter(
                checkbox => checkbox.checked
            )
            .map(
                checkbox =>
                    checkbox.dataset.path
            )
            .filter(Boolean);
    }


    function formatBytes(bytes) {
        let value = Number(
            bytes || 0
        );

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


    function renderPlan(plan) {
        const tapeWord =
            Number(plan.tape_count) === 1
            ? "tape"
            : "tapes";

        const spanText =
            plan.spanning_required
            ? "Yes"
            : "No";

        planStatus.textContent =
            `${plan.tape_count} LTO-6 ${tapeWord} estimated`;

        planDetails.style.display =
            "block";

        planDetails.innerHTML = `
            <div>
                <strong>Files:</strong>
                ${plan.file_count}
            </div>

            <div>
                <strong>Total size:</strong>
                ${formatBytes(plan.total_bytes)}
            </div>

            <div>
                <strong>Usable per tape:</strong>
                ${formatBytes(plan.capacity_bytes)}
            </div>

            <div>
                <strong>Estimated tapes:</strong>
                ${plan.tape_count}
            </div>

            <div>
                <strong>Spanning required:</strong>
                ${spanText}
            </div>

            <div>
                <strong>Files requiring split:</strong>
                ${plan.spanning_file_count}
            </div>

            <div>
                <strong>Estimated free on final tape:</strong>
                ${formatBytes(
                    plan.final_tape_free_bytes
                )}
            </div>
        `;
    }


    async function calculatePlan() {
        const paths =
            selectedPaths();

        const requestNumber =
            ++plannerRequest;

        if (!paths.length) {
            planPanel.style.display =
                "none";

            planDetails.style.display =
                "none";

            return;
        }

        planPanel.style.display =
            "block";

        planStatus.textContent =
            "Calculating archive requirements...";

        planDetails.style.display =
            "none";

        try {
            const response =
                await fetch(
                    "/api/staging/plan",
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json"
                        },
                        body: JSON.stringify({
                            paths
                        })
                    }
                );

            const data =
                await response.json();

            if (
                requestNumber
                !== plannerRequest
            ) {
                return;
            }

            if (
                !response.ok
                || !data.success
            ) {
                throw new Error(
                    data.error
                    || "Archive planning failed."
                );
            }

            renderPlan(
                data.plan
            );

        } catch (error) {
            if (
                requestNumber
                !== plannerRequest
            ) {
                return;
            }

            planStatus.textContent =
                error.message
                || "Archive planning failed.";

            planDetails.style.display =
                "none";
        }
    }


    function updateSelection() {
        const selected =
            selectedPaths();

        const count =
            selected.length;

        summary.textContent =
            count
            + (
                count === 1
                ? " item selected"
                : " items selected"
            );

        clearAllButton.disabled =
            count === 0;

        archiveSelectedButton.disabled =
            count === 0;

        selectAllButton.disabled =
            checkboxes().length === 0;

        if (plannerTimer) {
            clearTimeout(
                plannerTimer
            );
        }

        plannerTimer =
            setTimeout(
                calculatePlan,
                250
            );
    }


    document.addEventListener(
        "change",
        event => {
            if (
                !event.target.matches(
                    ".staging-select-checkbox"
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


    archiveSelectedButton.addEventListener(
        "click",
        async () => {
            const paths =
                selectedPaths();

            if (!paths.length) {
                return;
            }

            const originalText =
                archiveSelectedButton.textContent;

            archiveSelectedButton.disabled = true;
            archiveSelectedButton.textContent =
                "Starting Archive...";

            try {
                const response = await fetch(
                    "/api/staging/archive-selected",
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json",
                        },
                        body: JSON.stringify({
                            paths: paths,
                        }),
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
                        || "Could not start archive."
                    );
                }

                const operationId =
                    data.operation_id;

                if (!operationId) {
                    throw new Error(
                        "TapeBox did not return "
                        + "an operation ID."
                    );
                }

                const panel =
                    document.getElementById(
                        "archive-status-panel"
                    );

                const message =
                    document.getElementById(
                        "archive-status-message"
                    );

                const detail =
                    document.getElementById(
                        "archive-status-detail"
                    );

                if (panel) {
                    panel.style.display =
                        "block";
                }

                if (message) {
                    message.textContent =
                        "Selected archive started...";
                }

                if (detail) {
                    detail.textContent =
                        paths.length
                        + (
                            paths.length === 1
                            ? " selected item"
                            : " selected items"
                        );
                }

                archiveSelectedButton.textContent =
                    "Archive Running...";

                window.monitorTapeBoxArchiveOperation(
                    operationId
                );

            } catch (error) {
                alert(
                    error.message
                    || String(error)
                );

                archiveSelectedButton.disabled =
                    false;

                archiveSelectedButton.textContent =
                    originalText;
            }
        }
    );


    updateSelection();
})();
