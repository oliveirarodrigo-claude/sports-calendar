#!/usr/bin/swift
// update_calendar.swift
// Writes sports events to "Sports on TV" via EventKit (no Calendar.app needed).
//
// JSON format:
// {
//   "meta": {
//     "delete_prefixes": ["⚽️ 🦊 Cruzeiro", "🎾 João Fonseca"],   // title prefixes to wipe
//     "delete_tags":     ["wc2026", "furia-cs2"]                    // cruzeiro-calendar:// tags to wipe
//   },
//   "events": [{title, start_iso, end_iso, is_allday, availability?, calendar_tag?}, ...]
// }
//
// If "meta" is absent, falls back to deleting all known prefixes+tags (backward compat).

import EventKit
import Foundation

func iso(_ s: String) -> Date? {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = f.date(from: s) { return d }
    f.formatOptions = [.withInternetDateTime]
    return f.date(from: s)
}

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: update_calendar <fixtures.json>\n", stderr)
    exit(1)
}

let jsonPath = CommandLine.arguments[1]
guard let jsonData = try? Data(contentsOf: URL(fileURLWithPath: jsonPath)),
      let root = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] else {
    fputs("Error: could not read JSON from \(jsonPath)\n", stderr)
    exit(1)
}

// ── Parse payload ─────────────────────────────────────────────────────────────
let meta     = root["meta"] as? [String: Any]
let fixtures = root["events"] as? [[String: Any]] ?? []

// What to delete — from meta if present, otherwise full defaults
let deletePrefixes: [String]
let deleteTags:     [String]

if let meta = meta {
    deletePrefixes = meta["delete_prefixes"] as? [String] ?? []
    deleteTags     = meta["delete_tags"]     as? [String] ?? []
} else {
    // Backward-compat: full wipe of all known sports
    deletePrefixes = ["⚽️ 🦊 Cruzeiro", "🎾 João Fonseca"]
    deleteTags     = ["wc2026", "furia-cs2"]
}

// ── Calendar access ───────────────────────────────────────────────────────────
let store  = EKEventStore()
let sema   = DispatchSemaphore(value: 0)
var granted = false

if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents { g, _ in granted = g; sema.signal() }
} else {
    store.requestAccess(to: .event) { g, _ in granted = g; sema.signal() }
}
sema.wait()

guard granted else {
    fputs("Calendar access denied.\n", stderr)
    exit(1)
}

guard let cal = store.calendars(for: .event).first(where: { $0.title == "Sports on TV" }) else {
    fputs("Calendar 'Sports on TV' not found.\n", stderr)
    exit(1)
}

// ── Selective deletion ────────────────────────────────────────────────────────
let now   = Date()
let oneYr = Calendar.current.date(byAdding: .year, value: 1, to: now)!
let twoYr = Calendar.current.date(byAdding: .year, value: 2, to: now)!

var toDelete: [EKEvent] = []

if !deletePrefixes.isEmpty {
    let pred = store.predicateForEvents(withStart: now, end: oneYr, calendars: [cal])
    let byPrefix = store.events(matching: pred).filter { ev in
        // Use endDate so in-progress events can be replaced with updated times,
        // but truly completed events (endDate < now) are never deleted.
        ev.endDate > now &&
        deletePrefixes.contains(where: { ev.title.hasPrefix($0) })
    }
    toDelete += byPrefix
}

if !deleteTags.isEmpty {
    let pred = store.predicateForEvents(withStart: now, end: twoYr, calendars: [cal])
    let byTag = store.events(matching: pred).filter { ev in
        guard let urlStr = ev.url?.absoluteString else { return false }
        return ev.endDate > now &&
               deleteTags.contains(where: { urlStr == "cruzeiro-calendar://\($0)" })
    }
    toDelete += byTag
}

var deleted = 0
for ev in toDelete {
    if (try? store.remove(ev, span: .thisEvent)) != nil { deleted += 1 }
}

// ── Add new events ────────────────────────────────────────────────────────────
var added = 0
for f in fixtures {
    guard let title    = f["title"]     as? String,
          let startStr = f["start_iso"] as? String,
          let endStr   = f["end_iso"]   as? String,
          let startDate = iso(startStr),
          let endDate   = iso(endStr) else { continue }

    let isAllDay    = f["is_allday"]    as? Bool   ?? false
    let availStr    = f["availability"] as? String ?? ""
    let calendarTag = f["calendar_tag"] as? String ?? ""
    let notes       = f["notes"]        as? String ?? ""

    let ev = EKEvent(eventStore: store)
    ev.calendar  = cal
    ev.title     = title
    ev.startDate = startDate
    ev.endDate   = endDate
    ev.isAllDay  = isAllDay
    ev.availability = (availStr == "free") ? .free : (isAllDay ? .free : .busy)

    if !calendarTag.isEmpty {
        ev.url = URL(string: "cruzeiro-calendar://\(calendarTag)")
    }

    if !notes.isEmpty {
        ev.notes = notes
    }

    if (try? store.save(ev, span: .thisEvent)) != nil { added += 1 }
}

print("✅ \(added) events written to \"Sports on TV\" (deleted \(deleted) · CalendarAgent)")
