
document.addEventListener("click", async function (event) {
    const button = event.target.closest(".archive-button");

    if (!button) {
        return;
    }

    event.preventDefault();
    event.stopPropagation();

    const path = button.dataset.path;

    if (!path) {
        alert("TapeBox: Archive path is missing.");
        return;
    }

    if (!confirm(`Archive "${path}" to the currently loaded tape?`)) {
        return;
    }

    const panel = document.getElementById("archive-status-panel");
    const message = document.getElementById("archive-status-message");
    const detail = document.getElementById("archive-status-detail");

    if (panel) {
        panel.style.display = "block";
    }

    if (message) {
        message.textContent = "Starting archive...";
    }

    if (detail) {
        detail.textContent = `Source: ${path}`;
    }

    document.querySelectorAll(".archive-button").forEach((item) => {
        item.disabled = true;
    });

    try {
        const form = new FormData();
        form.append("path", path);

        const response = await fetch("/api/staging/archive", {
            method: "POST",
            body: form
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(
                data.error || "Could not start archive."
            );
        }

        const operationId = data.operation_id;

        if (message) {
            message.textContent = "Archive started...";
        }

        async function poll() {
            try {
                const statusResponse = await fetch(
                    `/api/operations/${operationId}`
                );

                const statusData = await statusResponse.json();

                if (!statusResponse.ok || !statusData.success) {
                    throw new Error(
                        statusData.error ||
                        "Could not read archive status."
                    );
                }

                const operation = statusData.operation;

                if (message) {
                    message.textContent =
                        operation.message ||
                        operation.status ||
                        "Working...";
                }

                if (detail) {
                    const lines = [
                        `Status: ${operation.status}`
                    ];

                    if (operation.job_id) {
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

                    detail.textContent = lines.join("\n");
                }

                if (
                    operation.status === "completed" ||
                    operation.status === "failed" ||
                    operation.status === "waiting_for_tape"
                ) {
                    document
                        .querySelectorAll(".archive-button")
                        .forEach((item) => {
                            item.disabled = false;
                        });

                    return;
                }

                setTimeout(poll, 1500);

            } catch (error) {
                if (message) {
                    message.textContent =
                        "Archive status error.";
                }

                if (detail) {
                    detail.textContent = error.message;
                }

                document
                    .querySelectorAll(".archive-button")
                    .forEach((item) => {
                        item.disabled = false;
                    });
            }
        }

        poll();

    } catch (error) {
        if (message) {
            message.textContent =
                "Archive failed to start.";
        }

        if (detail) {
            detail.textContent = error.message;
        }

        document.querySelectorAll(".archive-button").forEach((item) => {
            item.disabled = false;
        });
    }
});
