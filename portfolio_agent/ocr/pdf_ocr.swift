// Reads the text of a PDF whose pages are images (a scan, or a "print to PDF" copy),
// with macOS's own Vision text recognition. Runs on this Mac only; nothing is sent
// anywhere. Usage: pdf_ocr <file.pdf> [max pages]. Prints "[page N]" then each line,
// words on the same line kept together (left to right), so table rows stay intact.
import Foundation
import PDFKit
import Vision

let args = CommandLine.arguments
guard args.count > 1, let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    FileHandle.standardError.write("Can't open the PDF.\n".data(using: .utf8)!)
    exit(2)
}
if doc.isLocked && !doc.unlock(withPassword: "") {
    FileHandle.standardError.write("The PDF is password-protected.\n".data(using: .utf8)!)
    exit(3)
}
let maxPages = args.count > 2 ? (Int(args[2]) ?? 50) : 50
var out = ""
for i in 0..<min(doc.pageCount, maxPages) {
    guard let page = doc.page(at: i) else { continue }
    let box = page.bounds(for: .mediaBox)
    let scale: CGFloat = 3
    let w = Int(box.width * scale), h = Int(box.height * scale)
    guard w > 0, h > 0, let ctx = CGContext(
        data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { continue }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    guard let image = ctx.makeImage() else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false          // amounts and dates, not prose
    try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    let found: [(CGRect, String)] = (request.results ?? []).compactMap { o in
        guard let t = o.topCandidates(1).first?.string else { return nil }
        return (o.boundingBox, t)
    }
    var lines: [[(CGRect, String)]] = []
    for o in found.sorted(by: { $0.0.midY > $1.0.midY }) {     // top of the page first
        if let ref = lines.last?.first,
           abs(ref.0.midY - o.0.midY) < max(ref.0.height, o.0.height) * 0.5 {
            lines[lines.count - 1].append(o)
        } else {
            lines.append([o])
        }
    }
    out += "[page \(i + 1)]\n"
    for line in lines {
        out += line.sorted(by: { $0.0.minX < $1.0.minX }).map { $0.1 }.joined(separator: "   ") + "\n"
    }
    out += "\n"
}
print(out, terminator: "")
