#!/usr/bin/env bash

# Split every PDF inside Docs/ by calling splitpdfs.py.
# The script can be run from anywhere; paths are resolved relative to this file.

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DOCS_DIR="$SCRIPT_DIR/Docs"
SPLITS_DIR="$SCRIPT_DIR/splits"
SPLITTER="$SCRIPT_DIR/splitpdfs.py"

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Error: Python was not found. Install Python 3 and try again." >&2
    exit 1
fi

if [[ ! -d "$DOCS_DIR" ]]; then
    echo "Error: Docs folder not found: $DOCS_DIR" >&2
    exit 1
fi

if [[ ! -f "$SPLITTER" ]]; then
    echo "Error: splitpdfs.py not found: $SPLITTER" >&2
    exit 1
fi

mkdir -p "$SPLITS_DIR"

is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

pdf_count=0
success_count=0
failure_count=0

# File descriptor 3 is used for the find results so interactive prompts can
# continue reading from the terminal, even when PDF names contain spaces.
while IFS= read -r -d '' pdf_file <&3; do
    pdf_count=$((pdf_count + 1))
    pdf_name="$(basename -- "$pdf_file")"

    echo
    echo "============================================================"
    echo "PDF: $pdf_name"
    echo "Path: $pdf_file"
    echo "============================================================"

    while true; do
        read -r -p "Start page [1]: " start_page </dev/tty
        start_page="${start_page:-1}"

        if is_positive_integer "$start_page"; then
            break
        fi

        echo "Please enter a positive page number."
    done

    while true; do
        read -r -p "End page [last page]: " end_page </dev/tty

        if [[ -z "$end_page" ]]; then
            break
        fi

        if ! is_positive_integer "$end_page"; then
            echo "Please enter a positive page number, or press Enter for the last page."
            continue
        fi

        if (( end_page < start_page )); then
            echo "End page cannot be smaller than start page ($start_page)."
            continue
        fi

        break
    done

    while true; do
        read -r -p "Pages per report: " pages_per_report </dev/tty

        if is_positive_integer "$pages_per_report"; then
            break
        fi

        echo "Please enter a positive number."
    done

    command_args=(
        "$PYTHON_BIN"
        "$SPLITTER"
        "$pdf_file"
        --start "$start_page"
        --pages "$pages_per_report"
        --outdir "$SPLITS_DIR"
    )

    if [[ -n "$end_page" ]]; then
        command_args+=(--end "$end_page")
    fi

    echo
    echo "Running splitpdfs.py..."

    if "${command_args[@]}"; then
        success_count=$((success_count + 1))
    else
        failure_count=$((failure_count + 1))
        echo "Failed to split: $pdf_name" >&2
    fi

done 3< <(find "$DOCS_DIR" -type f -iname '*.pdf' -print0)

if (( pdf_count == 0 )); then
    echo "No PDF files were found inside: $DOCS_DIR"
    exit 0
fi

echo
echo "============================================================"
echo "Finished"
echo "PDFs found: $pdf_count"
echo "Successful: $success_count"
echo "Failed: $failure_count"
echo "Output folder: $SPLITS_DIR"
echo "============================================================"

if (( failure_count > 0 )); then
    exit 1
fi
