import os
import argparse
from pypdf import PdfReader, PdfWriter


def split_pdf(pdf_path, start, end, pages_per_report, outdir):

    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    if end is None:
        end = total_pages

    base = os.path.splitext(os.path.basename(pdf_path))[0]

    # اگر مسیر خروجی داده نشده بود، کنار PDF اصلی بساز
    if outdir is None:
        outdir = os.path.dirname(os.path.abspath(pdf_path))

    # فولدر مادر با نام PDF
    main_output_dir = os.path.join(outdir, base)

    os.makedirs(main_output_dir, exist_ok=True)

    counter = 1

    for p in range(start - 1, end, pages_per_report):

        writer = PdfWriter()

        last = min(p + pages_per_report, end)

        # اضافه کردن صفحات
        for page in range(p, last):
            writer.add_page(reader.pages[page])


        # نام گزارش
        report_name = f"{base}_{counter:03d}"

        # فولدر اختصاصی گزارش
        report_folder = os.path.join(
            main_output_dir,
            report_name
        )

        os.makedirs(report_folder, exist_ok=True)


        # مسیر PDF خروجی
        output_file = os.path.join(
            report_folder,
            f"{report_name}.pdf"
        )


        # ذخیره PDF
        with open(output_file, "wb") as f:
            writer.write(f)


        print(
            f"Created: {report_name}.pdf  "
            f"pages {p+1}-{last}"
        )

        counter += 1


    print()
    print(f"Done: {counter-1} reports")
    print(f"Output folder: {main_output_dir}")



if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Split PDF into separate report folders"
    )


    parser.add_argument(
        "pdf",
        help="Input PDF file"
    )


    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="First page"
    )


    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="Last page"
    )


    parser.add_argument(
        "--pages",
        type=int,
        default=3,
        help="Pages per report"
    )


    parser.add_argument(
        "--outdir",
        default=None,
        help="Main output folder"
    )


    args = parser.parse_args()


    split_pdf(
        args.pdf,
        args.start,
        args.end,
        args.pages,
        args.outdir
    )