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
            await window.tapeboxAlert({
                title:
                    "Archive Error",

                message:
                    "Archive path is missing.",

                type:
                    "error",
            });

            return;
        }

        const confirmed =
            await window.tapeboxConfirm({
                title:
                    "Archive to Tape?",

                message:
                    `Archive "${path}" to the `
                    + "currently loaded tape?",

                warning:
                    "TapeBox will begin writing "
                    + "this item to tape.",

                confirmText:
                    "Archive",

                danger: false,
            });

        if (!confirmed) {
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
// Resumable web upload into the current staging folder.
//
(function () {
    const CHUNK_SIZE =
        64 * 1024 * 1024;

    const MAX_RETRIES = 3;

    const zone =
        document.getElementById(
            "staging-upload-zone"
        );

    const browse =
        document.getElementById(
            "staging-upload-browse"
        );

    const folderBrowse =
        document.getElementById(
            "staging-upload-folder-browse"
        );

    const input =
        document.getElementById(
            "staging-upload-input"
        );

    const folderInput =
        document.getElementById(
            "staging-upload-folder-input"
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
        !folderBrowse ||
        !input ||
        !folderInput
    ) {
        return;
    }


    let uploadActive = false;


    function formatUploadBytes(bytes) {
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


    function stagingPathForFile(file) {
        const current =
            String(
                window.TAPEBOX_STAGING_PATH
                || ""
            )
            .replace(/\\/g, "/")
            .replace(/^\/+|\/+$/g, "");

        //
        // Folder selections provide webkitRelativePath:
        //
        //   Vacation/Day1/clip.mov
        //
        // Ordinary file selections fall back to file.name.
        //
        const relative =
            String(
                file.webkitRelativePath
                || file.name
                || ""
            )
            .replace(/\\/g, "/")
            .replace(/^\/+|\/+$/g, "");

        if (!relative) {
            throw new Error(
                "Invalid upload filename."
            );
        }

        if (!current) {
            return relative;
        }

        return `${current}/${relative}`;
    }


    function setProgress(
        received,
        total
    ) {
        const safeTotal =
            Number(total || 0);

        const safeReceived =
            Math.min(
                Number(received || 0),
                safeTotal
            );

        const percent =
            safeTotal > 0
                ? (
                    safeReceived
                    / safeTotal
                ) * 100
                : 100;

        if (progressBar) {
            progressBar.style.width =
                `${percent}%`;
        }

        if (progressText) {
            progressText.textContent =
                `${percent.toFixed(1)}% — ${
                    formatUploadBytes(
                        safeReceived
                    )
                } / ${
                    formatUploadBytes(
                        safeTotal
                    )
                }`;
        }
    }


    async function responseJson(
        response
    ) {
        try {
            return await response.json();
        } catch (error) {
            return null;
        }
    }


    async function sha256Blob(blob) {
        //
        // Web Crypto is normally unavailable when TapeBox is
        // opened from another machine over plain HTTP.
        //
        // In that case the server still hashes every received
        // chunk and performs a whole-file SHA-256 at completion.
        //
        if (
            !window.crypto
            || !window.crypto.subtle
        ) {
            return null;
        }

        const buffer =
            await blob.arrayBuffer();

        const digest =
            await window.crypto.subtle.digest(
                "SHA-256",
                buffer
            );

        return Array.from(
            new Uint8Array(digest)
        )
            .map(
                value =>
                    value
                    .toString(16)
                    .padStart(2, "0")
            )
            .join("");
    }


    async function createUpload(file) {
        const response =
            await fetch(
                "/api/staging/uploads/create",
                {
                    method: "POST",
                    headers: {
                        "Content-Type":
                            "application/json"
                    },
                    body: JSON.stringify({
                        path:
                            stagingPathForFile(
                                file
                            ),
                        size: file.size
                    })
                }
            );

        const data =
            await responseJson(
                response
            );

        if (
            !response.ok
            || !data
            || !data.success
        ) {
            throw new Error(
                data && data.error
                    ? data.error
                    : "Could not create upload."
            );
        }

        return data;
    }


    async function sendChunk(
        uploadId,
        offset,
        chunk,
        chunkHash
    ) {
        let lastError = null;

        for (
            let attempt = 1;
            attempt <= MAX_RETRIES;
            attempt++
        ) {
            try {
                const response =
                    await fetch(
                        `/api/staging/uploads/${
                            uploadId
                        }/chunk`,
                        {
                            method: "POST",
                            headers: {
                                "X-TapeBox-Offset":
                                    String(offset),

                                ...(
                                    chunkHash
                                        ? {
                                            "X-TapeBox-Chunk-SHA256":
                                                chunkHash
                                        }
                                        : {}
                                ),

                                "Content-Type":
                                    "application/octet-stream"
                            },
                            body: chunk
                        }
                    );

                const data =
                    await responseJson(
                        response
                    );

                //
                // This can happen if the server committed
                // a chunk but the browser lost the response.
                //
                if (
                    response.status === 409
                    && data
                    && Number.isFinite(
                        Number(
                            data.expected_offset
                        )
                    )
                ) {
                    return {
                        expectedOffset:
                            Number(
                                data.expected_offset
                            )
                    };
                }

                if (
                    !response.ok
                    || !data
                    || !data.success
                ) {
                    throw new Error(
                        data && data.error
                            ? data.error
                            : "Chunk upload failed."
                    );
                }

                return {
                    bytesReceived:
                        Number(
                            data.bytes_received
                        )
                };

            } catch (error) {
                lastError = error;

                if (
                    attempt < MAX_RETRIES
                ) {
                    await new Promise(
                        resolve =>
                            window.setTimeout(
                                resolve,
                                1000 * attempt
                            )
                    );
                }
            }
        }

        throw (
            lastError
            || new Error(
                "Chunk upload failed."
            )
        );
    }


    async function completeUpload(
        uploadId
    ) {
        const response =
            await fetch(
                `/api/staging/uploads/${
                    uploadId
                }/complete`,
                {
                    method: "POST"
                }
            );

        const data =
            await responseJson(
                response
            );

        if (
            !response.ok
            || !data
            || !data.success
        ) {
            throw new Error(
                data && data.error
                    ? data.error
                    : "Could not finalize upload."
            );
        }

        return data;
    }


    const SMALL_FILE_LIMIT =
        16 * 1024 * 1024;

    const BATCH_MAX_FILES = 250;
    const BATCH_MAX_BYTES =
        64 * 1024 * 1024;


    function makeUploadBatches(files) {
        const batches = [];

        let current = [];
        let currentBytes = 0;

        for (const file of files) {
            const size =
                Number(
                    file.size || 0
                );

            if (
                current.length
                && (
                    current.length
                        >= BATCH_MAX_FILES
                    || currentBytes + size
                        > BATCH_MAX_BYTES
                )
            ) {
                batches.push(current);
                current = [];
                currentBytes = 0;
            }

            current.push(file);
            currentBytes += size;
        }

        if (current.length) {
            batches.push(current);
        }

        return batches;
    }


    async function uploadSmallFileBatch(
        files,
        completedBytes,
        totalBytes
    ) {
        const formData =
            new FormData();

        let batchBytes = 0;

        for (const file of files) {
            const relativePath =
                file.webkitRelativePath
                || file.name;

            formData.append(
                "files",
                file,
                file.name
            );

            formData.append(
                "paths",
                relativePath
            );

            batchBytes +=
                Number(
                    file.size || 0
                );
        }

        if (message) {
            message.textContent =
                `Uploading ${
                    files.length
                } small files...`;
        }

        const response =
            await fetch(
                "/api/staging/uploads/batch",
                {
                    method: "POST",
                    body: formData,
                }
            );

        const data =
            await responseJson(
                response
            );

        if (
            !response.ok
            || !data
            || !data.success
        ) {
            throw new Error(
                data && data.error
                    ? data.error
                    : "Batch upload failed."
            );
        }

        setProgress(
            completedBytes + batchBytes,
            totalBytes
        );

        return {
            bytes: batchBytes,
            files: files.length,
            uploaded:
                Number(
                    data.uploaded || 0
                ),
            skipped:
                Number(
                    data.skipped || 0
                ),
        };
    }



    async function uploadOneFile(
        file,
        fileNumber,
        fileCount,
        completedBytes,
        totalBytes
    ) {
        if (message) {
            message.textContent =
                `Preparing ${fileNumber} of ${
                    fileCount
                }: ${file.name}`;
        }

        const created =
            await createUpload(file);

        if (
            created.already_exists
        ) {
            if (message) {
                message.textContent =
                    `Already present ${fileNumber} of ${
                        fileCount
                    }: ${file.name}`;
            }

            setProgress(
                completedBytes + file.size,
                totalBytes
            );

            return {
                skipped: true,
                alreadyExists: true,
            };
        }

        const upload =
            created.upload || {};

        const uploadId =
            upload.upload_id;

        if (!uploadId) {
            throw new Error(
                "Server did not return an upload ID."
            );
        }

        let offset =
            Number(
                upload.bytes_received || 0
            );

        if (
            offset < 0
            || offset > file.size
        ) {
            throw new Error(
                "Server returned an invalid resume offset."
            );
        }

        if (
            created.resumed
            && message
        ) {
            message.textContent =
                `Resuming ${fileNumber} of ${
                    fileCount
                }: ${file.name} at ${
                    formatUploadBytes(
                        offset
                    )
                }`;
        }

        setProgress(
            completedBytes + offset,
            totalBytes
        );

        while (
            offset < file.size
        ) {
            const end =
                Math.min(
                    offset + CHUNK_SIZE,
                    file.size
                );

            const chunk =
                file.slice(
                    offset,
                    end
                );

            if (message) {
                message.textContent =
                    `Uploading ${fileNumber} of ${
                        fileCount
                    }: ${file.name}`;
            }

            const chunkHash =
                await sha256Blob(
                    chunk
                );

            const result =
                await sendChunk(
                    uploadId,
                    offset,
                    chunk,
                    chunkHash
                );

            if (
                result.expectedOffset
                !== undefined
            ) {
                const expected =
                    Number(
                        result.expectedOffset
                    );

                if (
                    expected < 0
                    || expected > file.size
                ) {
                    throw new Error(
                        "Server returned an invalid recovery offset."
                    );
                }

                offset = expected;

                setProgress(
                    completedBytes
                        + offset,
                    totalBytes
                );

                continue;
            }

            offset =
                Number(
                    result.bytesReceived
                );

            setProgress(
                completedBytes + offset,
                totalBytes
            );
        }

        if (message) {
            message.textContent =
                `Verifying ${file.name}...`;
        }

        return await completeUpload(
            uploadId
        );
    }


    async function uploadFiles(files) {
        const selected =
            Array.from(
                files || []
            );

        if (
            !selected.length
            || uploadActive
        ) {
            return;
        }

        uploadActive = true;

        const totalBytes =
            selected.reduce(
                (
                    total,
                    file
                ) =>
                    total
                    + Number(
                        file.size || 0
                    ),
                0
            );

        const smallFiles =
            selected.filter(
                file =>
                    Number(
                        file.size || 0
                    )
                    <= SMALL_FILE_LIMIT
            );

        const largeFiles =
            selected.filter(
                file =>
                    Number(
                        file.size || 0
                    )
                    > SMALL_FILE_LIMIT
            );

        const batches =
            makeUploadBatches(
                smallFiles
            );

        let completedBytes = 0;
        let completedFiles = 0;

        if (status) {
            status.style.display =
                "block";
        }

        setProgress(
            0,
            totalBytes
        );

        browse.disabled = true;
        folderBrowse.disabled = true;
        input.disabled = true;
        folderInput.disabled = true;

        try {
            for (const batch of batches) {
                const result =
                    await uploadSmallFileBatch(
                        batch,
                        completedBytes,
                        totalBytes
                    );

                completedBytes +=
                    result.bytes;

                completedFiles +=
                    result.files;

                setProgress(
                    completedBytes,
                    totalBytes
                );

                if (message) {
                    message.textContent =
                        `Uploaded ${
                            completedFiles
                        } of ${
                            selected.length
                        } files`;
                }
            }

            for (
                let index = 0;
                index < largeFiles.length;
                index++
            ) {
                const file =
                    largeFiles[index];

                await uploadOneFile(
                    file,
                    completedFiles
                        + index
                        + 1,
                    selected.length,
                    completedBytes,
                    totalBytes
                );

                completedBytes +=
                    Number(
                        file.size || 0
                    );

                setProgress(
                    completedBytes,
                    totalBytes
                );
            }

            if (progressBar) {
                progressBar.style.width =
                    "100%";
            }

            if (progressText) {
                progressText.textContent =
                    `100.0% — ${
                        formatUploadBytes(
                            totalBytes
                        )
                    } / ${
                        formatUploadBytes(
                            totalBytes
                        )
                    }`;
            }

            if (message) {
                message.textContent =
                    `Upload complete — ${
                        selected.length
                    } file${
                        selected.length === 1
                            ? ""
                            : "s"
                    }.`;
            }

            window.setTimeout(
                () => {
                    window.location.reload();
                },
                900
            );

        } catch (error) {
            if (message) {
                message.textContent =
                    `Upload paused: ${
                        error.message
                        || "Unknown error."
                    }`;
            }

        } finally {
            uploadActive = false;

            browse.disabled = false;
            folderBrowse.disabled = false;
            input.disabled = false;
            folderInput.disabled = false;
        }
    }


    browse.addEventListener(
        "click",
        (event) => {
            event.stopPropagation();

            if (!uploadActive) {
                input.click();
            }
        }
    );


    folderBrowse.addEventListener(
        "click",
        (event) => {
            event.preventDefault();
            event.stopPropagation();

            if (uploadActive) {
                return;
            }

            //
            // Explicitly force directory-selection mode.
            // Chrome/Edge expose this as webkitdirectory.
            //
            folderInput.setAttribute(
                "webkitdirectory",
                ""
            );

            folderInput.webkitdirectory = true;

            if (
                typeof folderInput.showPicker
                === "function"
            ) {
                folderInput.showPicker();
            } else {
                folderInput.click();
            }
        }
    );


    zone.addEventListener(
        "click",
        (event) => {
            if (uploadActive) {
                return;
            }

            //
            // Buttons and inputs inside the drop zone manage
            // their own picker behavior.
            //
            if (
                event.target.closest(
                    "button, input"
                )
            ) {
                return;
            }

            input.click();
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


    folderInput.addEventListener(
        "change",
        () => {
            uploadFiles(
                folderInput.files
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

            if (!uploadActive) {
                uploadFiles(
                    event.dataTransfer.files
                );
            }
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

    const deleteSelectedButton =
        document.getElementById(
            "staging-delete-selected-button"
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
        || !deleteSelectedButton
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

        const generationName =
            plan.generation_name
            || "LTO";

        planStatus.textContent =
            `${plan.tape_count} ${generationName} ${tapeWord} estimated`;

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

        deleteSelectedButton.disabled =
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


    deleteSelectedButton.addEventListener(
        "click",
        async () => {
            const paths =
                selectedPaths();

            if (!paths.length) {
                return;
            }

            const count = paths.length;

            const confirmed =
                await window.tapeboxConfirm({
                    title:
                        "Delete from Staging?",

                    message:
                        "Permanently delete "
                        + count
                        + (
                            count === 1
                            ? " selected item?"
                            : " selected items?"
                        ),

                    warning:
                        "This cannot be undone.",

                    confirmText:
                        "Delete",

                    danger: true,
                });

            if (!confirmed) {
                return;
            }

            const originalText =
                deleteSelectedButton.textContent;

            deleteSelectedButton.disabled =
                true;

            archiveSelectedButton.disabled =
                true;

            selectAllButton.disabled =
                true;

            clearAllButton.disabled =
                true;

            deleteSelectedButton.textContent =
                "Deleting...";

            try {
                const response = await fetch(
                    "/api/staging/delete-selected",
                    {
                        method: "POST",
                        headers: {
                            "Content-Type":
                                "application/json",
                        },
                        body: JSON.stringify({
                            paths,
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
                        || "Could not delete "
                        + "selected staging items."
                    );
                }

                window.location.reload();

            } catch (error) {
                await window.tapeboxAlert({
                    title:
                        "Delete Failed",

                    message:
                        error.message
                        || String(error),

                    type:
                        "error",
                });

                deleteSelectedButton.textContent =
                    originalText;

                updateSelection();
            }
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
                await window.tapeboxAlert({
                    title:
                        "Archive Failed",

                    message:
                        error.message
                        || String(error),

                    type:
                        "error",
                });

                archiveSelectedButton.disabled =
                    false;

                archiveSelectedButton.textContent =
                    originalText;
            }
        }
    );


    updateSelection();
})();
