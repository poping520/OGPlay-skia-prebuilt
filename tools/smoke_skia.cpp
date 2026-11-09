#include "include/codec/SkCodec.h"
#include "include/codec/SkEncodedImageFormat.h"
#include "include/core/SkBitmap.h"
#include "include/core/SkCanvas.h"
#include "include/core/SkColor.h"
#include "include/core/SkData.h"
#include "include/core/SkFont.h"
#include "include/core/SkFontMetrics.h"
#include "include/core/SkFontMgr.h"
#include "include/core/SkPaint.h"
#include "include/core/SkPath.h"
#include "include/core/SkPathBuilder.h"
#include "include/core/SkPixmap.h"
#include "include/core/SkStream.h"
#include "include/core/SkString.h"
#include "include/core/SkSurface.h"
#include "include/core/SkTypeface.h"
#include "include/encode/SkJpegEncoder.h"
#include "include/encode/SkPngEncoder.h"
#include "include/pathops/SkPathOps.h"
#include "include/ports/SkFontMgr_empty.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

class CountingStream final : public SkWStream {
public:
    bool write(const void*, size_t size) override {
        bytes_written_ += size;
        return true;
    }

    size_t bytesWritten() const override { return bytes_written_; }

private:
    size_t bytes_written_ = 0;
};

class BitmapAllocator final : public SkBitmap::Allocator {
public:
    bool allocPixelRef(SkBitmap* bitmap) override {
        pixels_.resize(bitmap->computeByteSize() / sizeof(SkPMColor));
        bitmap->setPixels(pixels_.data());
        return true;
    }

    const SkPMColor* pixels() const { return pixels_.data(); }

private:
    std::vector<SkPMColor> pixels_;
};

void test_rtti_subclasses() {
    BitmapAllocator allocator;
    SkBitmap bitmap;
    require(bitmap.setInfo(SkImageInfo::MakeN32Premul(16, 12)), "custom bitmap info");
    require(bitmap.tryAllocPixels(&allocator), "custom bitmap allocation");
    require(bitmap.getPixels() == allocator.pixels(), "bitmap did not use custom storage");

    SkCanvas canvas(bitmap);
    canvas.clear(SK_ColorWHITE);
    SkPaint paint;
    paint.setColor(SK_ColorRED);
    canvas.drawRect(SkRect::MakeLTRB(2, 2, 10, 10), paint);
    require(bitmap.getColor(0, 0) == SK_ColorWHITE && bitmap.getColor(3, 3) == SK_ColorRED, "custom bitmap pixels");

    CountingStream stream;
    require(SkPngEncoder::Encode(&stream, bitmap.pixmap(), {}), "PNG encode to custom stream");
    const auto reference = SkPngEncoder::Encode(bitmap.pixmap(), {});
    require(reference && stream.bytesWritten() > 0 && stream.bytesWritten() == reference->size(),
            "custom stream PNG byte count");
    std::cout << "RTTI subclass OK\n";
}

void test_pathops() {
    const auto first = SkRect::MakeLTRB(0, 0, 10, 10);
    const auto second = SkRect::MakeLTRB(5, 5, 15, 15);
    const auto a = SkPath::Rect(first);
    const auto b = SkPath::Rect(second);

    const auto intersection = Op(a, b, kIntersect_SkPathOp);
    require(intersection && intersection->contains(6, 6) && !intersection->contains(2, 2) &&
                !intersection->contains(12, 12),
            "PathOps intersection");

    SkPathBuilder contours;
    contours.setFillType(SkPathFillType::kEvenOdd).addRect(first).addRect(second);
    const auto simplified = Simplify(contours.detach());
    require(simplified && simplified->contains(2, 2) && !simplified->contains(6, 6) && simplified->contains(12, 12),
            "PathOps simplify");

    SkOpBuilder builder;
    builder.add(a, kUnion_SkPathOp);
    builder.add(b, kUnion_SkPathOp);
    const auto combined = builder.resolve();
    require(combined && combined->contains(2, 2) && combined->contains(6, 6) && combined->contains(12, 12) &&
                !combined->contains(2, 12),
            "PathOps builder union");
    std::cout << "PathOps OK\n";
}

void test_raster_and_font(const char* font_path) {
    const auto info = SkImageInfo::MakeN32Premul(200, 100);
    auto surface = SkSurfaces::Raster(info);
    require(bool(surface), "could not create N32 premul raster surface");
    auto& canvas = *surface->getCanvas();
    canvas.clear(SK_ColorWHITE);

    SkPaint paint;
    paint.setColor(SK_ColorRED);
    canvas.drawRect(SkRect::MakeLTRB(10, 10, 40, 35), paint);

    paint.setAntiAlias(true);
    paint.setColor(SK_ColorBLUE);
    paint.setStrokeWidth(4);
    paint.setStyle(SkPaint::kStroke_Style);
    canvas.drawRoundRect(SkRect::MakeLTRB(55, 10, 95, 38), 8, 8, paint);

    paint.setStyle(SkPaint::kFill_Style);
    paint.setColor(SK_ColorGREEN);
    const SkPoint points[] = {{110.25f, 9.5f}, {149.5f, 33.25f}, {110.25f, 38.5f}};
    canvas.drawPath(SkPath::Polygon(points, true), paint);

    auto manager = SkFontMgr_New_Custom_Empty();
    // M157 installs one unnamed, empty placeholder family.
    require(manager && manager->countFamilies() == 1, "expected one placeholder font family");
    SkString family;
    manager->getFamilyName(0, &family);
    require(family.isEmpty(), "custom empty font manager exposed a named family");
    auto bytes = SkData::MakeFromFileName(font_path);
    require(bool(bytes), "could not read ROM font");
    auto typeface = manager->makeFromData(std::move(bytes));
    require(bool(typeface), "FreeType could not load the ROM font bytes");
    SkFont font(std::move(typeface), 24.0f);
    font.setEdging(SkFont::Edging::kAntiAlias);
    SkFontMetrics metrics;
    font.getMetrics(&metrics);
    require(metrics.fAscent < 0 && metrics.fDescent > 0 && std::isfinite(metrics.fLeading), "unexpected font metrics");
    paint.setColor(SK_ColorBLACK);
    canvas.drawString("OGPlay 4.4", 10, 80, font, paint);

    SkBitmap bitmap;
    bitmap.allocPixels(info);
    require(surface->readPixels(bitmap.pixmap(), 0, 0), "could not read raster pixels");
    require(bitmap.getColor(0, 0) == SK_ColorWHITE, "raster background");
    require(bitmap.getColor(20, 20) == SK_ColorRED, "filled rectangle");
    require(bitmap.getColor(75, 10) == SK_ColorBLUE, "rounded rectangle stroke");
    require(bitmap.getColor(75, 20) == SK_ColorWHITE, "rounded rectangle interior");
    require(bitmap.getColor(55, 10) == SK_ColorWHITE, "rounded rectangle corner");
    require(bitmap.getColor(120, 28) == SK_ColorGREEN, "path interior");

    int antialias_pixels = 0;
    for (int y = 8; y < 40; ++y) {
        for (int x = 109; x < 151; ++x) {
            const auto color = bitmap.getColor(x, y);
            antialias_pixels += color != SK_ColorWHITE && color != SK_ColorGREEN;
        }
    }
    require(antialias_pixels > 0, "path produced no partially covered pixels");

    int text_pixels = 0;
    for (int y = 54; y < 90; ++y) {
        for (int x = 9; x < 195; ++x) {
            text_pixels += bitmap.getColor(x, y) != SK_ColorWHITE;
        }
    }
    require(text_pixels > 0, "text region stayed white");
    std::cout << "Raster OK, path AA pixels=" << antialias_pixels << '\n';
    std::cout << "Font OK, text pixels=" << text_pixels << ", ascent=" << metrics.fAscent
              << ", descent=" << metrics.fDescent << ", leading=" << metrics.fLeading << '\n';
}

SkBitmap decode(sk_sp<SkData> data, SkEncodedImageFormat format, const char* label) {
    require(bool(data), std::string(label) + " input is empty");
    auto codec = SkCodec::MakeFromData(std::move(data));
    require(codec && codec->getEncodedFormat() == format, std::string(label) + " decoder");
    require(codec->dimensions() == SkISize::Make(16, 12), std::string(label) + " dimensions");
    SkBitmap bitmap;
    bitmap.allocPixels(SkImageInfo::MakeN32Premul(16, 12));
    require(codec->getPixels(bitmap.pixmap()) == SkCodec::kSuccess, std::string(label) + " pixels");
    return bitmap;
}

int color_error(SkColor a, SkColor b) {
    return std::max(
        {std::abs(int(SkColorGetA(a)) - int(SkColorGetA(b))), std::abs(int(SkColorGetR(a)) - int(SkColorGetR(b))),
         std::abs(int(SkColorGetG(a)) - int(SkColorGetG(b))), std::abs(int(SkColorGetB(a)) - int(SkColorGetB(b)))});
}

int check_pixels(const SkBitmap& bitmap, const SkBitmap* reference, int tolerance, const char* label) {
    const auto expected = SkColorSetRGB(32, 128, 224);
    int max_error = 0;
    for (int y = 0; y < bitmap.height(); ++y) {
        for (int x = 0; x < bitmap.width(); ++x) {
            const auto color = reference ? reference->getColor(x, y) : expected;
            max_error = std::max(max_error, color_error(bitmap.getColor(x, y), color));
        }
    }
    require(max_error <= tolerance, std::string(label) + " max channel error " + std::to_string(max_error));
    return max_error;
}

void test_codecs(const char* png_path, const char* jpeg_path, const char* gif_path, const char* webp_path) {
    const auto png = decode(SkData::MakeFromFileName(png_path), SkEncodedImageFormat::kPNG, "PNG");
    check_pixels(png, nullptr, 0, "PNG");
    const auto png_again = decode(SkPngEncoder::Encode(png.pixmap(), {}), SkEncodedImageFormat::kPNG, "PNG round-trip");
    const int png_error = check_pixels(png_again, &png, 0, "PNG round-trip");

    const auto jpeg = decode(SkData::MakeFromFileName(jpeg_path), SkEncodedImageFormat::kJPEG, "JPEG");
    check_pixels(jpeg, nullptr, 6, "JPEG");
    SkJpegEncoder::Options options;
    options.fDownsample = SkJpegEncoder::Downsample::k444;
    const auto jpeg_again =
        decode(SkJpegEncoder::Encode(jpeg.pixmap(), options), SkEncodedImageFormat::kJPEG, "JPEG round-trip");
    const int jpeg_error = check_pixels(jpeg_again, &jpeg, 6, "JPEG round-trip");

    const auto gif = decode(SkData::MakeFromFileName(gif_path), SkEncodedImageFormat::kGIF, "GIF");
    check_pixels(gif, nullptr, 0, "GIF");
    const auto webp = decode(SkData::MakeFromFileName(webp_path), SkEncodedImageFormat::kWEBP, "WebP");
    check_pixels(webp, nullptr, 0, "WebP");

    std::cout << "Codecs OK, PNG/JPEG 16x12 round-trip max channel errors=" << png_error << '/' << jpeg_error
              << ", GIF/WebP decode OK\n";
}
}  // namespace

int main(int argc, char** argv) {
    if (argc != 6) {
        std::cerr << "Usage: smoke_skia FONT PNG JPEG GIF WEBP\n";
        return EXIT_FAILURE;
    }
    try {
        test_raster_and_font(argv[1]);
        test_codecs(argv[2], argv[3], argv[4], argv[5]);
        test_rtti_subclasses();
        test_pathops();
    } catch (const std::exception& error) {
        std::cerr << "Skia smoke failed: " << error.what() << '\n';
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
