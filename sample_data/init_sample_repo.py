"""Generator for realistic HIS 315 staging repository (.md, .docx, .pdf, .cs, .ts, .tsx + git)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from docx import Document

# Canary secrets and PII tokens used by automated tests to verify 0% leak across DB & logs
CANARY_DB_PASSWORD = "HisProdSuperSecret!2026"
CANARY_OPENAI_KEY = "sk-proj-CanarySecretKey315DoNotLeak999999"
CANARY_PATIENT_NAME_1 = "Nguyễn Văn An"
CANARY_PATIENT_NAME_2 = "Trần Thị Bích Ngọc"
CANARY_CCCD_1 = "079095001234"
CANARY_BHYT_1 = "DN4797912345678"
CANARY_BHYT_2 = "HX4797998765432"
CANARY_PHONE_1 = "0909123456"
CANARY_EMAIL_1 = "nguyenvanan85@gmail.com"


def _build_simple_pdf(pages_text: list[str], output_path: Path) -> None:
    """Create a valid multi-page PDF document readable by pypdf without external font files."""
    objects: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_obj_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_obj_ids: list[int] = []
    content_obj_ids: list[int] = []

    for page_text in pages_text:
        lines = page_text.strip().splitlines()
        stream_lines = [b"BT", b"/F1 11 Tf", b"50 760 Td", b"14 TL"]
        for line in lines:
            escaped = (
                line.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)")
                .encode("latin-1", errors="replace")
            )
            stream_lines.append(b"(" + escaped + b") Tj T*")
        stream_lines.append(b"ET")
        stream_data = b"\n".join(stream_lines)
        content_id = add_obj(
            f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
            + stream_data
            + b"\nendstream"
        )
        content_obj_ids.append(content_id)

    pages_tree_id = len(objects) + len(pages_text) + 1
    for content_id in content_obj_ids:
        page_id = add_obj(
            f"<< /Type /Page /Parent {pages_tree_id} 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_obj_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode("ascii")
        )
        page_obj_ids.append(page_id)

    kids_str = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    actual_pages_tree_id = add_obj(
        f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_obj_ids)} >>".encode("ascii")
    )
    assert actual_pages_tree_id == pages_tree_id
    catalog_id = add_obj(f"<< /Type /Catalog /Pages {pages_tree_id} 0 R >>".encode("ascii"))

    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for idx, obj_bytes in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{idx} 0 obj\n".encode("ascii"))
        output.extend(obj_bytes)
        output.extend(b"\nendobj\n")

    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        output.extend(f"{off:010d} 00000 n \n".encode("ascii"))

    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(bytes(output))


def _write_markdown_docs(repo_dir: Path) -> None:
    md_path = repo_dir / "docs" / "quy_trinh_tiep_nhan_bhyt.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_lines = [
        "# 1. Quy trình tiếp nhận bệnh nhân ngoại trú và đăng ký khám BHYT (Module: Registration)",
        "",
        "Hệ thống HIS 315 thực hiện tiếp đón bệnh nhân ngoại trú tại quầy đăng ký hoặc Kiosk.",
        "Nhân viên tiếp đón quét mã QR trên thẻ BHYT hoặc căn cước công dân gắn chip.",
        "",
        "Ví dụ bản ghi thực tế từ môi trường staging:",
        f"Hôm qua tại quầy số 3, {CANARY_PATIENT_NAME_1} đến khám nội tổng quát với CCCD "
        f"{CANARY_CCCD_1}, mã thẻ BHYT {CANARY_BHYT_1}, số điện thoại {CANARY_PHONE_1}, "
        f"ngày sinh 15/08/1985, email {CANARY_EMAIL_1} và mã hồ sơ BA-2026-009812.",
        "",
        "## 2. Kiểm tra thông tuyến BHYT ngoại trú và quyền lợi 5 năm liên tục",
        "",
        "Khi đăng ký khám BHYT ngoại trú qua API `POST /api/v1/registration/outpatient-bhyt`:",
        "1. Kiểm tra hạn sử dụng thẻ BHYT (`CardValidFrom` đến `CardValidTo`). "
        "Nếu thẻ hết hạn quá 15 ngày thì từ chối hưởng BHYT.",
        "2. Kiểm tra điều kiện **5 năm liên tục** (`FiveYearContinuousDate`): nếu bệnh nhân đã "
        "tham gia BHYT đủ 5 năm liên tục và có số tiền cùng chi trả trong năm lớn hơn "
        "6 tháng lương cơ sở (14,040,000 VND) thì miễn đồng chi trả (`CopayRate = 0%`).",
        "3. Đối với trường hợp cấp cứu (`IsEmergency = true`), bệnh nhân được hưởng đúng tuyến.",
        "",
        "## 3. Bảng mã quyền lợi và tỷ lệ hưởng BHYT ngoại trú",
        "",
        "| Mã mức hưởng | Nhóm đối tượng | Quỹ BHYT thanh toán | Bệnh nhân đồng chi trả |",
        "|---|---|---|---|",
        "| Mức 1 (DN1, HX1, TE1) | Trẻ em dưới 6 tuổi, ưu tiên | 100% chi phí | 0% đồng chi trả |",
        "| Mức 2 (CH2, NN2, TK2) | Hưu trí, cận nghèo | 95% chi phí | 5% đồng chi trả |",
        "| Mức 4 (DN4, HX4, GD4) | Người lao động, hộ gia đình | 80% chi phí | 20% đồng chi trả |",
        "",
    ]
    md_path.write_text("\n".join(md_lines), encoding="utf-8")


def _write_docx_docs(repo_dir: Path) -> None:
    docx_path = repo_dir / "docs" / "quy_dinh_ke_don_va_cap_phat_thuoc.docx"
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading(
        "1. Quy dinh ke don thuoc ngoai tru va canh bao tuong tac thuoc (Pharmacy)",
        level=1,
    )
    doc.add_paragraph(
        "Bac si ke don thuoc dien tu tren phan he Kham benh (Outpatient) ket noi truc tiep "
        "voi kho Duoc (Pharmacy) qua API POST /api/v1/pharmacy/prescriptions/validate-interaction. "
        "He thong bat buoc kiem tra tuong tac thuoc chong chi dinh va di ung khang sinh."
    )
    doc.add_paragraph(
        f"Log kiem thu mẫu: Sản phụ {CANARY_PATIENT_NAME_2} có thẻ BHYT {CANARY_BHYT_2} "
        f"và SĐT 0912345678 được cảnh báo tương tác hoạt chất Warfarin + Aspirin."
    )

    doc.add_heading("2. Bang phan loai canh bao tuong tac thuoc va xuat kho duoc", level=2)
    table = doc.add_table(rows=1, cols=3)
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = "Muc do tuong tac"
    hdr_cells[1].text = "Hanh vi he thong HIS"
    hdr_cells[2].text = "API xu ly"

    rows_data = [
        (
            "Muc nghiem trong (Contraindicated)",
            "Chan luu don thuoc cho den khi bac si doi thuoc hoac nhap ly do ghi de",
            "POST /api/v1/pharmacy/prescriptions/validate-interaction",
        ),
        (
            "Xuat kho cap phat thuoc BHYT",
            "Tru ton kho FIFO theo lo han dung va xac nhan phieu xuat kho",
            "POST /api/v1/pharmacy/dispense/confirm",
        ),
    ]
    for level_str, behavior_str, api_str in rows_data:
        row_cells = table.add_row().cells
        row_cells[0].text = level_str
        row_cells[1].text = behavior_str
        row_cells[2].text = api_str

    doc.save(str(docx_path))


def _write_pdf_docs(repo_dir: Path) -> None:
    pdf_path = repo_dir / "docs" / "bang_gia_vien_phi_va_thanh_toan_bhyt.pdf"
    page_1 = "\n".join(
        [
            "1. QUY TRINH DONG CHI TRA VA THANH TOAN VIEN PHI BHYT (MODULE: BILLING)",
            "He thong Vien phi (Billing) tinh toan chi phi kham chua benh ngoai tru BHYT",
            "thong qua API POST /api/v1/billing/bhyt-settlement/calculate.",
            "Cong thuc tinh dong chi tra (CopayAmount):",
            "- Neu tong chi phi dot kham nho hon 15% luong co so (351,000 VND): Copay = 0.",
            "- Neu benh nhan dat dieu kien 5 nam lien tuc (FiveYearContinuous) va luy ke",
            "  dong chi tra vuot 6 thang luong co so (14,040,000 VND): Copay = 0.",
            "- Nguoc lai: ap dung ty le dong chi tra theo ma the BHYT (20% cho DN4/GD4).",
        ]
    )
    page_2 = "\n".join(
        [
            "2. BANG THAM SO QUY KHAM CHUA BENH BHYT VA TRAN THANH TOAN",
            "| Tham so he thong | Ma cau hinh | Gia tri ap dung |",
            "| Luong co so BHYT | BASE_SALARY_VND | 2340000 VND |",
            "| Nguong mien dong chi tra 15% | FREE_COPAY_THRESHOLD_VND | 351000 VND |",
            "| Nguong 6 thang luong co so (5 nam) | SIX_MONTH_BASE_SALARY_VND | 14040000 |",
            "API doi soat: BhytSettlementController.CalculateBhytCopayAsync trong HIS.Billing.",
        ]
    )
    _build_simple_pdf([page_1, page_2], pdf_path)


def _write_source_code(repo_dir: Path) -> None:
    reg_ctrl_path = (
        repo_dir
        / "src"
        / "HIS.Registration.Api"
        / "Controllers"
        / "PatientRegistrationController.cs"
    )
    reg_ctrl_path.parent.mkdir(parents=True, exist_ok=True)
    reg_ctrl_lines = [
        "using System;",
        "using System.Threading.Tasks;",
        "using Microsoft.AspNetCore.Mvc;",
        "using HIS.Registration.Api.Entities;",
        "",
        "namespace HIS.Registration.Api.Controllers",
        "{",
        "    [ApiController]",
        '    [Route("api/v1/registration")]',
        "    public class PatientRegistrationController : ControllerBase",
        "    {",
        "        // Canary connection string & API key to test DLP redaction during ingestion",
        '        private const string LegacyConn = "Server=10.31.5.99;Database=HIS_PROD;'
        f'User Id=sa;Password={CANARY_DB_PASSWORD};";',
        f'        private const string AiGatewayKey = "{CANARY_OPENAI_KEY}";',
        "",
        '        [HttpPost("outpatient-bhyt")]',
        "        public async Task<IActionResult> RegisterOutpatientBhytAsync(",
        "            [FromBody] OutpatientVisit request)",
        "        {",
        "            // Note: braces in comments { should be ignored by lexer }",
        '            string tpl = $"Checking BHYT {request.BhytCardNumber} with {{braces}}";',
        '            string verbatimSql = @"SELECT * FROM Visits WHERE Meta=\'{""a"":1}\'";',
        "            if (string.IsNullOrWhiteSpace(request.BhytCardNumber))",
        "            {",
        '                return BadRequest(new { ErrorCode = "MISSING_BHYT", Msg = tpl });',
        "            }",
        "            if (request.CardValidTo < DateTime.UtcNow.Date)",
        "            {",
        '                return UnprocessableEntity(new { Error = "EXPIRED", Sql = verbatimSql });',
        "            }",
        "            bool qualifiesFiveYear = request.FiveYearContinuousDate.HasValue",
        "                && request.FiveYearContinuousDate.Value <= DateTime.UtcNow.Date",
        "                && request.AccumulatedCopayVnd >= 14040000m;",
        "            decimal copayRate = qualifiesFiveYear ? 0.0m : 0.20m;",
        "            return Ok(new",
        "            {",
        "                VisitId = Guid.NewGuid(),",
        "                CopayRate = copayRate,",
        "                FiveYearApplied = qualifiesFiveYear",
        "            });",
        "        }",
        "",
        '        [HttpGet("bhyt-five-year-check/{cardNo}")]',
        "        public async Task<IActionResult> VerifyFiveYearContinuousBhytAsync(",
        "            [FromRoute] string cardNo)",
        "        {",
        "            bool isValidFormat = !string.IsNullOrEmpty(cardNo) && cardNo.Length == 15;",
        "            return Ok(new",
        "            {",
        "                CardNumber = cardNo,",
        "                IsValidFormat = isValidFormat,",
        '                CheckedBy = "HIS.Registration.Api"',
        "            });",
        "        }",
        "    }",
        "}",
        "",
    ]
    reg_ctrl_path.write_text("\n".join(reg_ctrl_lines), encoding="utf-8")

    entities_path = (
        repo_dir
        / "src"
        / "HIS.Registration.Api"
        / "Entities"
        / "OutpatientRegistrationEntities.cs"
    )
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_code = """using System;

namespace HIS.Registration.Api.Entities
{
    public class OutpatientVisit
    {
        public Guid Id { get; set; }
        public string PatientId { get; set; } = string.Empty;
        public string BhytCardNumber { get; set; } = string.Empty;
        public DateTime CardValidFrom { get; set; }
        public DateTime CardValidTo { get; set; }
        public DateTime? FiveYearContinuousDate { get; set; }
        public decimal AccumulatedCopayVnd { get; set; }
        public decimal CopayRate { get; set; }
        public bool IsEmergency { get; set; }
    }

    public class BhytCardInfo
    {
        public string CardNumber { get; set; } = string.Empty;
        public string BenefitLevelCode { get; set; } = "DN4";
        public string RegisteredHospitalCode { get; set; } = "79024";
        public DateTime? FiveYearContinuousDate { get; set; }
    }
}
"""
    entities_path.write_text(entities_code, encoding="utf-8")

    pharm_ctrl_path = (
        repo_dir
        / "src"
        / "HIS.Pharmacy.Api"
        / "Controllers"
        / "PrescriptionDispenseController.cs"
    )
    pharm_ctrl_path.parent.mkdir(parents=True, exist_ok=True)
    pharm_ctrl_code = """using System;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Mvc;

namespace HIS.Pharmacy.Api.Controllers
{
    [ApiController]
    [Route("api/v1/pharmacy")]
    public class PrescriptionDispenseController : ControllerBase
    {
        [HttpPost("prescriptions/validate-interaction")]
        public async Task<IActionResult> ValidateDrugInteractionAsync(
            [FromBody] object prescriptionPayload)
        {
            string ruleNote = "Contraindicated interaction check { Warfarin + Aspirin }";
            return Ok(new { HasContraindication = false, Rule = ruleNote });
        }

        [HttpPost("dispense/confirm")]
        public async Task<IActionResult> ConfirmMedicineDispenseAsync(
            [FromBody] object dispenseRequest)
        {
            return Ok(new { DispenseStatus = "CONFIRMED_FIFO", Timestamp = DateTime.UtcNow });
        }
    }
}
"""
    pharm_ctrl_path.write_text(pharm_ctrl_code, encoding="utf-8")

    billing_ctrl_path = (
        repo_dir
        / "src"
        / "HIS.Billing.Api"
        / "Controllers"
        / "BhytSettlementController.cs"
    )
    billing_ctrl_path.parent.mkdir(parents=True, exist_ok=True)
    billing_ctrl_code = """using System;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Mvc;

namespace HIS.Billing.Api.Controllers
{
    [ApiController]
    [Route("api/v1/billing")]
    public class BhytSettlementController : ControllerBase
    {
        private const decimal BaseSalaryVnd = 2340000m;
        private const decimal FreeCopayThresholdVnd = 351000m;
        private const decimal SixMonthBaseSalaryVnd = 14040000m;

        [HttpPost("bhyt-settlement/calculate")]
        public async Task<IActionResult> CalculateBhytCopayAsync(
            [FromBody] decimal totalVisitAmountVnd)
        {
            decimal copayAmount = totalVisitAmountVnd < FreeCopayThresholdVnd
                ? 0m
                : totalVisitAmountVnd * 0.20m;
            return Ok(new
            {
                TotalAmount = totalVisitAmountVnd,
                CopayAmount = copayAmount,
                BaseSalary = BaseSalaryVnd
            });
        }
    }
}
"""
    billing_ctrl_path.write_text(billing_ctrl_code, encoding="utf-8")

    tsx_path = repo_dir / "frontend" / "src" / "components" / "BhytRegistrationForm.tsx"
    tsx_path.parent.mkdir(parents=True, exist_ok=True)
    tsx_code = """import React from "react";

export function validateBhytCardFormat(cardNumber: string): boolean {
    const clean = cardNumber.trim().toUpperCase();
    const pattern = /^[A-Z]{2}[1-5]\\d{12}$/;
    return pattern.test(clean);
}

export function BhytRegistrationForm(props: { initialCard: string; isFiveYear: boolean }) {
    const badgeLabel = props.isFiveYear
        ? "Du 5 nam lien tuc {Mien dong chi tra}"
        : "BHYT thuong";
    return (
        <div className="bhyt-registration-form" data-module="Registration">
            <h3>Dang ky kham ngoai tru BHYT</h3>
            <span>{badgeLabel}</span>
        </div>
    );
}
"""
    tsx_path.write_text(tsx_code, encoding="utf-8")

    ts_path = repo_dir / "frontend" / "src" / "services" / "hisApiClient.ts"
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    ts_code = """export interface OutpatientBhytPayload {
    patientId: string;
    bhytCardNumber: string;
    fiveYearContinuousDate?: string;
}

export async function submitOutpatientBhytRegistration(
    payload: OutpatientBhytPayload
): Promise<string> {
    const endpoint = `/api/v1/registration/outpatient-bhyt`;
    return `${endpoint}:${payload.bhytCardNumber}`;
}
"""
    ts_path.write_text(ts_code, encoding="utf-8")

    # Create ignored artifacts in bin/, obj/, and node_modules/ to verify chunker exclusion
    bin_file = repo_dir / "src" / "HIS.Registration.Api" / "bin" / "Debug" / "IgnoredArtifact.cs"
    bin_file.parent.mkdir(parents=True, exist_ok=True)
    bin_file.write_text(
        "public class IgnoredBinArtifact { public void DoNotIndex() {} }",
        encoding="utf-8",
    )

    obj_file = repo_dir / "src" / "HIS.Registration.Api" / "obj" / "Debug" / "IgnoredObj.cs"
    obj_file.parent.mkdir(parents=True, exist_ok=True)
    obj_file.write_text(
        "public class IgnoredObjArtifact { public void DoNotIndex() {} }",
        encoding="utf-8",
    )

    nm_file = repo_dir / "frontend" / "node_modules" / "fake-lib" / "IgnoredModule.ts"
    nm_file.parent.mkdir(parents=True, exist_ok=True)
    nm_file.write_text(
        "export function ignoredNodeModuleFn() { return 'DO_NOT_INDEX'; }",
        encoding="utf-8",
    )


def _run_git(repo_dir: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def ensure_sample_git_repo(repo_path: str | Path, force_recreate: bool = False) -> str:
    """Idempotently initialize HIS 315 sample staging repo with main/develop/staging branches."""
    repo_dir = Path(repo_path).resolve()
    staging_ref = repo_dir / ".git" / "refs" / "heads" / "staging"
    if staging_ref.exists() and not force_recreate:
        return _run_git(repo_dir, ["rev-parse", "refs/heads/staging"])

    repo_dir.mkdir(parents=True, exist_ok=True)
    _write_markdown_docs(repo_dir)
    _write_docx_docs(repo_dir)
    _write_pdf_docs(repo_dir)
    _write_source_code(repo_dir)

    git_dir = repo_dir / ".git"
    if not git_dir.exists():
        _run_git(repo_dir, ["init", "-b", "main"])
        _run_git(repo_dir, ["config", "user.name", "HIS 315 Bot"])
        _run_git(repo_dir, ["config", "user.email", "his-bot@315.vn"])

    _run_git(repo_dir, ["add", "docs", "src", "frontend"])
    status_out = _run_git(repo_dir, ["status", "--porcelain"])
    if status_out:
        _run_git(
            repo_dir,
            ["commit", "-m", "feat(his): initial HIS 315 staging docs and source code"],
        )

    # Ensure main, develop, and staging branches all exist cleanly
    _run_git(repo_dir, ["branch", "-f", "develop", "HEAD"])
    _run_git(repo_dir, ["checkout", "-B", "staging", "HEAD"])

    return _run_git(repo_dir, ["rev-parse", "refs/heads/staging"])


if __name__ == "__main__":
    sha = ensure_sample_git_repo("./sample_data/his_staging")
    print(f"Initialized sample HIS staging repo at commit: {sha}")
