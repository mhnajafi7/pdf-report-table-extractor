#!/usr/bin/env bash

# Process split report PDFs with table_crop_bundle_pipeline.py.
#
# Expected repository layout:
#
#   repository/
#   ├── splits/
#   │   ├── SourceDocumentA/
#   │   │   ├── SourceDocumentA_001/
#   │   │   │   └── SourceDocumentA_001.pdf
#   │   │   └── SourceDocumentA_002/
#   │   │       └── SourceDocumentA_002.pdf
#   │   └── SourceDocumentB/
#   ├── Types/
#   │   ├── type1.txt
#   │   ├── type2.txt
#   │   └── ...
#   └── table_crop_bundle_pipeline.py
#
# For each top-level document group inside splits/, the script asks once which
# TXT type definition should be used. It then processes every report PDF in
# that group and stores the result in:
#
#   <report-folder>/table_bundle/

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SPLITS_DIR="$SCRIPT_DIR/splits"
TYPES_DIR="$SCRIPT_DIR/Types"
PIPELINE="$SCRIPT_DIR/table_crop_bundle_pipeline.py"

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Error: Python was not found. Install Python 3 and try again." >&2
    exit 1
fi

if [[ ! -d "$SPLITS_DIR" ]]; then
    echo "Error: splits folder not found: $SPLITS_DIR" >&2
    echo "Run 01_split_docs.sh first." >&2
    exit 1
fi

if [[ ! -d "$TYPES_DIR" ]]; then
    echo "Error: Types folder not found: $TYPES_DIR" >&2
    exit 1
fi

if [[ ! -f "$PIPELINE" ]]; then
    echo "Error: table_crop_bundle_pipeline.py not found: $PIPELINE" >&2
    exit 1
fi

# Load every TXT file directly inside Types/.
type_files=()
for type_file in "$TYPES_DIR"/*.txt; do
    if [[ -f "$type_file" ]]; then
        type_files+=("$type_file")
    fi
done

if (( ${#type_files[@]} == 0 )); then
    echo "Error: No TXT type files were found inside: $TYPES_DIR" >&2
    exit 1
fi

print_type_menu() {
    local index

    echo "Available report types:"
    for ((index = 0; index < ${#type_files[@]}; index++)); do
        printf "  %d) %s\n" \
            "$((index + 1))" \
            "$(basename -- "${type_files[$index]}")"
    done
    echo "  s) Skip this document group"
    echo "  q) Quit"
}

select_type_for_group() {
    local group_name="$1"
    local selection
    local selected_index

    while true; do
        echo
        echo "------------------------------------------------------------"
        echo "Document group: $group_name"
        echo "------------------------------------------------------------"
        print_type_menu
        echo

        read -r -p "Select report type: " selection </dev/tty

        case "$selection" in
            s|S)
                return 2
                ;;
            q|Q)
                return 3
                ;;
        esac

        if [[ "$selection" =~ ^[1-9][0-9]*$ ]]; then
            selected_index=$((selection - 1))

            if (( selected_index >= 0 && selected_index < ${#type_files[@]} )); then
                SELECTED_TYPE="${type_files[$selected_index]}"
                return 0
            fi
        fi

        echo "Invalid selection. Enter a menu number, s, or q."
    done
}

group_count=0
processed_group_count=0
skipped_group_count=0
pdf_count=0
success_count=0
failure_count=0
quit_requested=0

for group_dir in "$SPLITS_DIR"/*; do
    [[ -d "$group_dir" ]] || continue

    # Ignore hidden folders.
    group_name="$(basename -- "$group_dir")"
    [[ "$group_name" == .* ]] && continue

    report_pdfs=()

    # Exclude PDFs generated inside table_bundle directories, especially
    # map__all_tables.pdf from previous runs.
    while IFS= read -r -d '' pdf_file; do
        report_pdfs+=("$pdf_file")
    done < <(
        find "$group_dir" \
            -type f \
            -iname '*.pdf' \
            ! -path '*/table_bundle/*' \
            -print0
    )

    # A top-level directory with no source report PDFs is not a document group.
    if (( ${#report_pdfs[@]} == 0 )); then
        continue
    fi

    group_count=$((group_count + 1))

    SELECTED_TYPE=""
    select_type_for_group "$group_name"
    selection_status=$?

    if (( selection_status == 2 )); then
        skipped_group_count=$((skipped_group_count + 1))
        echo "Skipped group: $group_name"
        continue
    fi

    if (( selection_status == 3 )); then
        quit_requested=1
        echo "Stopped by user."
        break
    fi

    processed_group_count=$((processed_group_count + 1))

    echo
    echo "Using type: $(basename -- "$SELECTED_TYPE")"
    echo "Reports found in this group: ${#report_pdfs[@]}"

    for pdf_file in "${report_pdfs[@]}"; do
        pdf_count=$((pdf_count + 1))

        report_dir="$(dirname -- "$pdf_file")"
        report_name="$(basename -- "$pdf_file")"
        output_dir="$report_dir/table_bundle"

        echo
        echo "============================================================"
        echo "Processing: $report_name"
        echo "Type:       $(basename -- "$SELECTED_TYPE")"
        echo "Output:     $output_dir"
        echo "============================================================"

        command_args=(
            "$PYTHON_BIN"
            "$PIPELINE"
            "$pdf_file"
            --titles "$SELECTED_TYPE"
            --output "$output_dir"
        )

        if "${command_args[@]}"; then
            success_count=$((success_count + 1))
            echo "Completed: $report_name"
        else
            failure_count=$((failure_count + 1))
            echo "Failed: $report_name" >&2
        fi
    done
done

if (( group_count == 0 )); then
    echo "No split report PDFs were found inside: $SPLITS_DIR"
    exit 0
fi

echo
echo "============================================================"
echo "Finished"
echo "Document groups found:     $group_count"
echo "Document groups processed: $processed_group_count"
echo "Document groups skipped:   $skipped_group_count"
echo "Report PDFs processed:     $pdf_count"
echo "Successful:                $success_count"
echo "Failed:                    $failure_count"
echo "Output location:           each report folder/table_bundle"
echo "============================================================"

if (( quit_requested == 1 )); then
    exit 0
fi

if (( failure_count > 0 )); then
    exit 1
fi
