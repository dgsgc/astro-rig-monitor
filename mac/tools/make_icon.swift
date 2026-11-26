// Draws the app icon: a night-sky tile with a guide-star reticle and a calm guiding trace.
//
//     swift mac/tools/make_icon.swift      → mac/Resources/AppIcon.icns
//
// (mac/build.sh runs it when the icon is missing.)

import AppKit
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

func color(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(red: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

struct Seeded {
    var state: UInt64
    mutating func next() -> CGFloat {
        state = state &* 6364136223846793005 &+ 1442695040888963407
        return CGFloat((state >> 33) & 0xffffff) / CGFloat(0xffffff)
    }
}

/// Draws on a 1024-point canvas (y up), scaled to `size` pixels.
func draw(size: Int) -> CGImage {
    let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpace(name: CGColorSpace.sRGB)!,
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)

    // Tile (macOS icon grid: 824 pt square, 185 pt corner radius, centred).
    let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
    let path = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 24, color: color(0x000000, 0.35))
    ctx.addPath(path); ctx.setFillColor(color(0x0b1020)); ctx.fillPath()
    ctx.restoreGState()
    ctx.saveGState()
    ctx.addPath(path); ctx.clip()
    let grad = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                          colors: [color(0x1b2a4a), color(0x0b1020)] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(grad, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])

    // Faint stars.
    var rnd = Seeded(state: 42)
    for _ in 0..<70 {
        let x = 100 + rnd.next() * 824, y = 330 + rnd.next() * 594, r = 1.5 + rnd.next() * 3.5
        ctx.setFillColor(color(0xffffff, 0.25 + rnd.next() * 0.5))
        ctx.fillEllipse(in: CGRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r))
    }

    // Guide star in a reticle.
    let c = CGPoint(x: 512, y: 610)
    let glow = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                          colors: [color(0xfff6d8, 1), color(0xfff6d8, 0)] as CFArray, locations: [0, 1])!
    ctx.drawRadialGradient(glow, startCenter: c, startRadius: 0, endCenter: c, endRadius: 70, options: [])
    ctx.setStrokeColor(color(0x97c459)); ctx.setLineWidth(16)
    ctx.strokeEllipse(in: CGRect(x: c.x - 150, y: c.y - 150, width: 300, height: 300))
    ctx.setLineWidth(14); ctx.setLineCap(.round)
    for (dx, dy) in [(0.0, 1.0), (0.0, -1.0), (1.0, 0.0), (-1.0, 0.0)] {
        ctx.move(to: CGPoint(x: c.x + 180 * dx, y: c.y + 180 * dy))
        ctx.addLine(to: CGPoint(x: c.x + 95 * dx, y: c.y + 95 * dy))
    }
    ctx.strokePath()

    // Guiding trace: RA (blue) and Dec (orange), small and steady.
    for (hex, phase, amp) in [(UInt32(0x378add), 0.0, 26.0), (UInt32(0xd85a30), 1.7, 18.0)] {
        ctx.setStrokeColor(color(hex)); ctx.setLineWidth(12); ctx.setLineJoin(.round)
        var first = true
        for i in stride(from: 0.0, through: 1.0, by: 1.0 / 60) {
            let x = 170 + i * 684
            let y = 255 + amp * sin(i * 19 + phase) * (0.6 + 0.4 * sin(i * 5 + phase))
            if first { ctx.move(to: CGPoint(x: x, y: y)); first = false } else { ctx.addLine(to: CGPoint(x: x, y: y)) }
        }
        ctx.strokePath()
    }
    ctx.restoreGState()
    return ctx.makeImage()!
}

let here = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().deletingLastPathComponent()
let set = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("AppIcon.iconset")
try? FileManager.default.removeItem(at: set)
try FileManager.default.createDirectory(at: set, withIntermediateDirectories: true)
for (pts, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)] {
    let name = "icon_\(pts)x\(pts)\(scale == 2 ? "@2x" : "").png"
    let dest = CGImageDestinationCreateWithURL(set.appendingPathComponent(name) as CFURL,
                                               UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, draw(size: pts * scale), nil)
    CGImageDestinationFinalize(dest)
}
let out = here.appendingPathComponent("Resources/AppIcon.icns")
let p = Process()
p.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
p.arguments = ["-c", "icns", set.path, "-o", out.path]
try p.run(); p.waitUntilExit()
print(p.terminationStatus == 0 ? "wrote \(out.path)" : "iconutil failed")
