// Cards for every hazard kind. Each opens from a bare {kind, id} (favourites, case files, search,
// the AI) by asking /api/hazards/details, shows a loading card at once, then fills it in.

import { ACCENT, GLYPH, SEVERITY, imageryDates, clip, sentence, now, warningName } from './kit.js';
import { VOLCANO_LEVEL } from './volcanoes.js';

const KINDS = ['earthquake', 'fire', 'volcano', 'storm', 'flood', 'warning', 'outbreak', 'disaster', 'tsunami',
  'emergency_mapping', 'gauge'];
const ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI', 'XII'];
const TONE_ALERT = { Green: 'green', Orange: 'amber', Red: 'red' };
const TONE_SEV = { Extreme: 'red', Severe: 'amber', Moderate: 'amber', Minor: 'blue' };
const TONE_PAGER = { green: 'green', yellow: 'amber', orange: 'amber', red: 'red' };
const TONE_HANS = { GREEN: 'green', YELLOW: 'amber', ORANGE: 'amber', RED: 'red' };

export function roman(mmi) {
  if (mmi == null || Number.isNaN(+mmi) || +mmi < 1) return null;
  return ROMAN[Math.min(11, Math.max(0, Math.round(+mmi) - 1))];
}

export function registerCards(ctx, kit, extras) {
  const { h, fmt } = ctx;

  const loading = (entity) => ctx.cards.open({
    key: `${entity.kind}:${entity.id}`, kind: entity.kind, title: entity.title || 'Loading…', subtitle: entity.subtitle || '',
    icon: GLYPH[entity.kind] || '•', accent: ACCENT, sections: [{ text: 'Loading details…' }],
    entity: ctx.dom.validLatLon(entity.lat, entity.lon) ? entity : null,
  });

  async function open(entity) {
    if (!entity || !entity.id) return null;
    const key = `${entity.kind}:${entity.id}`;
    const card = loading(entity);
    let d;
    try {
      d = await ctx.api.get('/api/hazards/details', { kind: entity.kind, id: entity.id });
    } catch (err) {
      if (card.isOpen()) card.update({ sections: [{ text: `Details are unavailable right now (${err.message || err}).` }] });
      return card;
    }
    if (!card.isOpen()) return null;
    // the emergency mapping, England flood and gauge cards live in their own files (extras.builders)
    const build = BUILDERS[d.type] || extras.builders?.[d.type] || missingCard;
    let spec;
    try {
      spec = build(d, entity);
    } catch (err) {
      console.error('hazards card failed', err);
      spec = missingCard(d, entity);
    }
    spec.key = key;
    spec.kind = entity.kind;
    spec.accent = ACCENT;
    spec.icon = GLYPH[entity.kind] || '•';
    if (!d.live && d.last_seen) {
      spec.badges = [{ text: `Last seen ${fmt.ago(d.last_seen)}`, tone: 'grey', title: 'No longer in the live feed; this is the last copy Meridian saw.' }, ...(spec.badges || [])];
    }
    const opened = ctx.cards.open(spec);
    spec.after?.(opened);
    return opened;
  }

  for (const kind of KINDS) ctx.entities.handler(kind, open, { group: 'Events', icon: GLYPH[kind] });

  // -------------------------------------------------------------------- shared bits
  const ent = (d, entity) => {
    const e = { ...(d.entity || {}) };
    if (!ctx.dom.validLatLon(e.lat, e.lon) && ctx.dom.validLatLon(entity.lat, entity.lon)) { e.lat = entity.lat; e.lon = entity.lon; }
    e.kind = e.kind || entity.kind;
    e.id = e.id || entity.id;
    return ctx.dom.validLatLon(e.lat, e.lon) ? e : null;
  };
  const pos = (lat, lon) => (ctx.dom.validLatLon(lat, lon) ? fmt.latlon(+lat, +lon, 3) : null);
  const text = (t, cls = 'hazards-text') => h('div', { class: cls }, t);
  const entList = (items) => items.map(it => ({ title: it.entity.title, subtitle: it.subtitle || it.entity.subtitle,
    detail: it.detail || '', entity: it.entity }));
  const linkIf = (t, href) => (href ? { text: t, href } : null);

  function missingCard(d, entity) {
    const label = { earthquake: 'earthquake', fire: 'fire detection', volcano: 'volcano', storm: 'storm', flood: 'flood',
      warning: 'warning', outbreak: 'outbreak report', disaster: 'disaster alert', tsunami: 'tsunami message',
      emergency_mapping: 'mapping activation', gauge: 'gauge' }[entity.kind] || 'item';
    return {
      title: entity.title || `Unknown ${label}`, subtitle: entity.subtitle || 'Not in the live feeds',
      sections: [{ text: `This ${label} is no longer in the live feeds and Meridian has no saved copy of it.` }],
      entity: ctx.dom.validLatLon(entity.lat, entity.lon) ? entity : null,
    };
  }

  // -------------------------------------------------------------------- earthquake
  function quakeCard(d, entity) {
    const q = d.data;
    const badges = [];
    if (q.alert) badges.push({ text: `PAGER ${q.alert}`, tone: TONE_PAGER[q.alert], title: 'USGS PAGER estimate of losses from shaking' });
    if (q.tsunami) badges.push({ text: 'Tsunami flag', tone: 'blue', title: 'Large enough in an oceanic region to be checked for tsunami. Official messages come from tsunami.gov.' });
    if ((q.sig || 0) >= 600) badges.push({ text: 'Significant', tone: 'amber', title: `USGS significance ${q.sig}` });
    if (q.gdacs) badges.push({ text: `GDACS ${q.gdacs.alert.toLowerCase()}`, tone: TONE_ALERT[q.gdacs.alert] });
    badges.push({ text: q.status === 'reviewed' ? 'Reviewed by a seismologist' : 'Automatic solution', tone: 'grey' });
    if (q.type && q.type !== 'earthquake') badges.push({ text: cap(q.type), tone: 'grey' });
    const felt = q.felt ?? q.dyfi?.responses;
    const mmi = q.mmi ?? q.shakemap?.max_mmi ?? q.pager?.max_mmi;
    const sections = [{ rows: [
      ['Time', kit.when(q.time)],
      ['Position', pos(q.lat, q.lon)],
      ['Country or sea', q.country],
      ['Depth', q.depth_km != null ? `${fmt.num(q.depth_km, 1)} km${q.origin?.depth_type ? ` (${q.origin.depth_type})` : ''}` : null],
      ['Magnitude', q.mag != null ? `${fmt.num(q.mag, 1)} ${q.mag_type || ''}`.trim() : null],
      ['Felt reports', felt ? `${fmt.int(felt)} reports to Did You Feel It?${q.dyfi?.max_cdi ? `, strongest ${roman(q.dyfi.max_cdi)}` : ''}` : null],
      ['Shaking', mmi ? `Up to intensity ${roman(mmi)} (modified Mercalli)` : null],
      ['PAGER', q.pager?.alert ? `${cap(q.pager.alert)} alert for losses` : null],
      ['Faulting', q.moment_tensor?.style ? `${q.moment_tensor.style}${q.moment_tensor.type ? `, ${q.moment_tensor.type}` : ''}` : null],
      ['Location error', q.origin?.horizontal_error_km ? `± ${fmt.num(q.origin.horizontal_error_km, 1)} km` : null],
      ['Network', q.net ? q.net.toUpperCase() : null],
      ['Updated', q.updated ? fmt.ago(q.updated) : null],
    ] }];
    if (q.gdacs?.population) sections.push({ title: 'GDACS impact estimate', text: q.gdacs.population });
    if (q.tsunami_messages?.length) {
      sections.push({ title: 'Tsunami messages', list: q.tsunami_messages.map(t => ({ title: t.entity.title, subtitle: t.entity.subtitle,
        detail: fmt.ago(t.issued), entity: t.entity })) });
    }
    for (const t of q.texts || []) sections.push({ title: 'About', text: t });
    if (q.nearby?.length) {
      sections.push({ title: `Within 150 km in the week around it (${q.nearby_count})`, list: q.nearby.map(n => ({
        title: n.entity.title, subtitle: n.entity.subtitle, detail: fmt.km(n.distance_km), entity: n.entity })) });
    }
    const t = q.time;
    return {
      // USGS places can start in lower case ("west of Macquarie Island")
      title: q.place ? `${q.place.charAt(0).toUpperCase()}${q.place.slice(1)}` : 'Earthquake', subtitle: `Magnitude ${q.mag != null ? fmt.num(q.mag, 1) : '?'} ${q.type === 'earthquake' || !q.type ? 'earthquake' : q.type}, ${fmt.ago(t)}`,
      badges,
      block: [
        { label: 'Magnitude', value: q.mag != null ? fmt.num(q.mag, 1) : '–', unit: q.mag_type || '' },
        { label: 'Depth', value: q.depth_km != null ? fmt.int(q.depth_km) : '–', unit: 'km' },
        { label: 'Felt reports', value: felt != null ? fmt.compact(felt) : '0' },
        { label: 'Intensity', value: roman(mmi) || '–', unit: mmi ? 'MMI' : '' },
      ],
      media: q.shakemap?.image ? { type: 'image', src: q.shakemap.image, alt: 'USGS ShakeMap intensity map', credit: 'USGS ShakeMap' } : null,
      sections,
      links: [linkIf('USGS event page', q.url), linkIf('Did you feel it?', q.url && `${q.url}/tellus`),
        q.shakemap ? linkIf('ShakeMap', `${q.url}/shakemap`) : null, q.pager ? linkIf('PAGER', `${q.url}/pager`) : null,
        q.gdacs ? linkIf('GDACS report', q.gdacs.report_url) : null].filter(Boolean),
      actions: [kit.compareAction(q.lat, q.lon, 9, imageryDates(t, 7, 3))],
      entity: ent(d, entity),
      foot: `USGS Earthquake Hazards Program, updated ${fmt.ago(q.updated || t)}`,
    };
  }

  // -------------------------------------------------------------------- fire
  function fireCard(d, entity) {
    const f = d.data;
    const live = d.live !== false;
    const badges = [];
    if (f.confidence) badges.push({ text: `${f.confidence} confidence`, tone: f.confidence_rank === 2 ? 'amber' : 'grey' });
    if (f.night != null) badges.push({ text: f.night ? 'Night pass' : 'Day pass', tone: 'grey' });
    if (!live) badges.push({ text: 'Not in the 24-hour feed', tone: 'grey' });
    const brightName = f.instrument === 'MODIS' ? 'Brightness, 21 and 31' : 'Brightness, I4 and I5';
    const sections = [{ rows: [
      ['Detected', kit.when(f.time)],
      ['Position', pos(f.lat, f.lon)],
      ['Country', f.country],
      ['Satellite', `${f.satellite}${f.instrument ? `, ${f.instrument}` : ''}`],
      [brightName, f.brightness ? `${fmt.num(f.brightness, 1)} K${f.brightness2 ? ` and ${fmt.num(f.brightness2, 1)} K` : ''}` : null],
      ['Pixel', f.scan && f.track ? `${fmt.num(f.scan, 2)} by ${fmt.num(f.track, 2)} km` : null],
      ['Nearby', f.nearby_count != null ? (f.nearby_count ? `${fmt.int(f.nearby_count)} more detection${+f.nearby_count === 1 ? '' : 's'} within 5 km, ${fmt.int(f.nearby_frp)} MW in all` : 'No other detections within 5 km') : null],
    ] }, { title: 'About', text: 'Satellites see heat, not flames: most detections are vegetation fires, but volcanoes, gas flares and hot industrial sites show up too.' }];
    return {
      title: entity.title && entity.title !== 'Fire detection' ? entity.title : d.entity.title,
      subtitle: live ? `${f.satellite} ${f.instrument || ''}, ${fmt.ago(f.time)}` : `Detected ${fmt.utc(f.time)}, no longer in the 24-hour feed`,
      badges,
      block: [
        { label: 'Fire power', value: f.frp != null ? fmt.num(f.frp, 1) : '–', unit: 'MW' },
        { label: 'Brightness', value: f.brightness ? fmt.int(f.brightness) : '–', unit: 'K' },
        { label: 'Confidence', value: f.confidence || '–' },
      ],
      sections,
      links: [{ text: 'FIRMS fire map', href: `https://firms.modaps.eosdis.nasa.gov/map/#d:24hrs;@${(+f.lon).toFixed(3)},${(+f.lat).toFixed(3)},11.0z` }],
      actions: [kit.compareAction(f.lat, f.lon, 12, imageryDates(f.time, 10, 1))],
      entity: ent(d, entity),
      foot: 'NASA FIRMS near-real-time data. Times are satellite overpass times.',
    };
  }

  // -------------------------------------------------------------------- EONET natural event
  function naturalCard(d, entity) {
    const e = d.data;
    const track = e.track || [];
    const badges = [{ text: e.category_label, tone: 'grey' }, e.ended ? { text: 'Closed', tone: 'grey' } : { text: 'Open', tone: 'amber' }];
    const sections = [{ rows: [
      ['Started', kit.when(e.start)],
      ['Last report', track.length > 1 ? kit.when(e.time) : null],
      ['Position', pos(e.lat, e.lon)],
      ['Country', e.country],
      ['Size', e.magnitude != null ? `${fmt.num(e.magnitude, 2)} ${e.unit_label || ''}`.trim() : null],
      ['Closed', e.closed ? fmt.utc(e.closed) : null],
    ] }];
    if (e.description) sections.push({ title: 'About', text: e.description });
    if (track.length > 1) {
      sections.push({ title: `Track (${track.length} positions)`, list: track.slice(-8).reverse().map(p => ({
        title: fmt.utc(p.t), subtitle: pos(p.lat, p.lon), detail: p.mag != null ? `${fmt.num(p.mag, 0)} ${e.unit_label || ''}` : '' })) });
    }
    if (e.sources?.length) sections.push({ title: 'Reported by', list: e.sources.map(s => ({ title: s.name || s.id, subtitle: clip(s.url, 60) })) });
    return {
      title: e.title, subtitle: `${e.category_label}, NASA EONET${e.ended ? ', closed' : ''}`,
      badges,
      block: [
        e.magnitude != null ? { label: 'Size', value: fmt.num(e.magnitude, e.magnitude >= 100 ? 0 : 1), unit: e.unit_label } : null,
        startCell(e.start),
        { label: 'Positions', value: fmt.int(track.length) },
      ].filter(Boolean),
      sections,
      links: (e.sources || []).slice(0, 3).map(s => ({ text: s.name || 'Source', href: s.url })),
      actions: [kit.compareAction(e.lat, e.lon, e.category === 'severeStorms' ? 6 : 9, imageryDates(e.start, 5, Math.max(3, ((e.time || now()) - (e.start || now())) / 86400 + 2)))],
      entity: ent(d, entity),
      foot: 'NASA Earth Observatory Natural Event Tracker',
    };
  }

  // -------------------------------------------------------------------- GDACS disaster
  function disasterCard(d, entity) {
    const e = d.data;
    const badges = [{ text: `${e.alert} alert`, tone: TONE_ALERT[e.alert] || 'grey' },
      e.ongoing ? { text: 'Ongoing', tone: 'amber' } : { text: 'Ended', tone: 'grey' }];
    if (e.temporary) badges.push({ text: 'Provisional', tone: 'grey', title: 'GDACS may still revise this event' });
    const days = e.from && e.to ? Math.max(0, (e.to - e.from) / 86400) : null;
    const sev = e.severity != null && e.severity > 0 ? e.severity : null;
    const sevUnit = { M: '', 'km/h': 'km/h', ha: 'ha', km2: 'km²' }[e.severity_unit] ?? (e.severity_unit || '');
    const sevValue = sev == null ? null : sev >= 100000 ? fmt.compact(sev) : fmt.num(sev, sev >= 100 ? 0 : 1);
    const manyCountries = (e.country || '').split(',').length > 3;
    const sections = [];
    if (e.description) sections.push({ text: e.description });
    sections.push({ rows: [
      ['Hazard', e.type_name], ['Alert', `${e.alert}${e.episode_alert && e.episode_alert !== e.alert ? `, latest episode ${e.episode_alert.toLowerCase()}` : ''}`],
      ['Severity', e.severity_text], ['People', e.population], ['Countries', e.country], ['From', e.from ? fmt.utc(e.from) : null],
      ['To', e.to ? fmt.utc(e.to) : null], ['Source', e.source], ['GLIDE number', e.glide],
    ] });
    if (e.usgs) sections.push({ title: 'USGS record', list: [{ title: e.usgs.title, subtitle: e.usgs.subtitle, entity: e.usgs }] });
    if (e.emergency_mapping?.length) {
      sections.push({ title: 'Copernicus emergency mapping', list: e.emergency_mapping.map(m => ({ title: m.title, subtitle: m.subtitle, entity: m })) });
    }
    return {
      title: e.short && e.name.length > 60 ? e.short : e.name,
      subtitle: `${e.type_name}, GDACS ${e.alert.toLowerCase()} alert${e.country && !manyCountries ? `, ${clip(e.country, 60)}` : ''}`,
      badges,
      block: [
        sev != null ? { label: e.type === 'EQ' ? 'Magnitude' : e.type === 'TC' ? 'Max wind' : e.type === 'DR' ? 'Area' : e.type === 'WF' ? 'Burnt area' : 'Severity', value: sevValue, unit: sevUnit } : null,
        { label: 'Alert score', value: e.alert_score != null ? fmt.num(e.alert_score, 1) : '–' },
        { label: 'Countries', value: fmt.int((e.countries || []).length || (e.country ? e.country.split(',').length : 0)) },
        days != null ? { label: 'Duration', value: fmt.num(days, days < 10 ? 1 : 0), unit: 'days' } : null,
      ].filter(Boolean).slice(0, 4),
      media: e.image ? { type: 'image', src: e.image, alt: 'GDACS event map', credit: 'GDACS' } : null,
      sections,
      links: [linkIf('GDACS report', e.report_url)].filter(Boolean),
      actions: [kit.compareAction(e.lat, e.lon, e.type === 'DR' ? 5 : 8, imageryDates(e.from, 7, Math.max(3, days || 0) + 2))],
      entity: ent(d, entity),
      foot: 'GDACS, the Global Disaster Alert and Coordination System',
      after: (card) => extras.disasters?.showArea(card.key, e.id, e.alert),
    };
  }

  // -------------------------------------------------------------------- storm
  function stormCard(d, entity) {
    const s = d.data;
    const badges = [s.active ? { text: 'Active', tone: 'amber' } : { text: 'No recent advisory', tone: 'grey' }];
    if (s.alert) badges.push({ text: `GDACS ${s.alert.toLowerCase()}`, tone: TONE_ALERT[s.alert] });
    if (s.basin) badges.push({ text: s.basin, tone: 'grey' });
    const move = s.movement_dir != null ? `${compass(s.movement_dir)} at ${fmt.int(s.movement_kt)} kn` : null;
    const track = s.track || [];
    const sections = [{ rows: [
      ['Position', pos(s.lat, s.lon)], ['Last advisory', kit.when(s.time)], ['Strength', s.category_label],
      ['Max wind', s.wind_kmh ? `${fmt.int(s.wind_kmh)} km/h, ${fmt.int(s.wind_kt)} kn` : null],
      ['Pressure', s.pressure_mb ? `${fmt.int(s.pressure_mb)} hPa` : null], ['Moving', move],
      ['Countries', (s.countries || []).join(', ') || null], ['Advisory', s.advisory_number ? `Number ${s.advisory_number}` : null],
      ['People', s.population], ['Source', s.source_label],
    ] }];
    if (s.description) sections.push({ title: 'About', text: s.description });
    if (track.length) {
      const fc = track.filter(p => p.forecast);
      const past = track.filter(p => !p.forecast);
      const rows = [...fc.reverse().slice(0, 5), ...past.reverse().slice(0, 5)];
      sections.push({ title: fc.length ? 'Forecast and recent track' : 'Recent track', list: rows.map(p => ({
        title: p.label || (p.t ? fmt.utc(p.t) : 'Position'), subtitle: pos(p.lat, p.lon), detail: p.forecast ? 'Forecast' : 'Observed' })) });
    }
    return {
      title: s.title, subtitle: `${s.category_label}${s.wind_kmh ? `, ${fmt.int(s.wind_kmh)} km/h` : ''}, ${s.source_label}`,
      badges,
      block: [
        { label: 'Max wind', value: s.wind_kmh ? fmt.int(s.wind_kmh) : '–', unit: 'km/h' },
        { label: 'Category', value: s.category != null && s.category >= 1 ? String(s.category) : s.category === 0 ? 'TS' : s.category === -1 ? 'TD' : '–' },
        s.pressure_mb ? { label: 'Pressure', value: fmt.int(s.pressure_mb), unit: 'hPa' } : null,
        move ? { label: 'Moving', value: compass(s.movement_dir), unit: `${fmt.int(s.movement_kt)} kn` } : null,
      ].filter(Boolean),
      media: s.image ? { type: 'image', src: s.image, alt: 'GDACS storm map', credit: 'GDACS' } : null,
      sections,
      links: (s.links || []).filter(l => l && l.href),
      actions: [
        s.bbox ? { text: 'Show the whole track', onClick: () => ctx.map2.fitBbox(s.bbox, { maxZoom: 6 }) } : null,
        kit.compareAction(s.lat, s.lon, 6, imageryDates(s.from || (s.time ? s.time - 4 * 86400 : null), 3, 30)),
      ].filter(Boolean),
      entity: ent(d, entity),
      foot: s.source === 'nhc' ? 'NOAA National Hurricane Center' : s.source === 'gdacs' ? 'GDACS, from the Joint Typhoon Warning Center and national centres' : 'NASA EONET',
    };
  }

  // -------------------------------------------------------------------- volcano
  function volcanoCard(d, entity) {
    const v = d.data;
    const a = v.activity || {};
    const badges = [];
    // HANS sends "ORANGE" and "Watch"; sentence() leaves three-letter "RED" alone, so cap() the colour
    const hans = (codeWord, levelWord) => (a.hans ? [a.hans.color ? `${cap(a.hans.color)} ${codeWord}` : null,
      a.hans.level ? `${String(a.hans.level).toLowerCase()} ${levelWord}` : null].filter(Boolean).join(', ') : '');
    if (a.hans) badges.push({ text: hans('code', 'level') || 'USGS notice', tone: TONE_HANS[a.hans.color] || 'grey', title: a.hans.observatory || 'USGS' });
    if (a.eonet) badges.push({ text: 'Erupting or unrest (EONET)', tone: 'amber' });
    if (a.gdacs) badges.push({ text: `GDACS ${String(a.gdacs.alert || '').toLowerCase()}`, tone: TONE_ALERT[a.gdacs.alert] || 'grey' });
    if (!badges.length) badges.push({ text: 'No current alert', tone: 'grey' });
    const sections = [{ rows: [
      ['Country', v.country], ['Region', v.subregion || v.region], ['Type', v.type], ['Landform', v.landform],
      ['Rock', v.rock], ['Tectonic setting', v.tectonic], ['Evidence', v.evidence], ['Position', pos(v.lat, v.lon)],
    ] }];
    const act = [];
    if (a.hans) act.push({ title: `${a.hans.observatory || 'USGS'}: ${hans('aviation code', 'alert level') || 'notice'}`, subtitle: a.hans.sent ? `Notice ${fmt.ago(a.hans.sent)}` : '' });
    if (a.eonet) act.push({ title: a.eonet.title, subtitle: `NASA EONET, open since ${fmt.utc(a.eonet.start)}` });
    if (a.gdacs) act.push({ title: a.gdacs.name, subtitle: `GDACS ${String(a.gdacs.alert).toLowerCase()} alert, ${fmt.ago(a.gdacs.time)}` });
    if (act.length) sections.push({ title: 'Current activity', list: act });
    if (v.summary) sections.push({ title: 'About', el: expandable(v.summary, 520) });
    if (v.recent_quakes?.length) {
      sections.push({ title: `Earthquakes within 40 km this week (${v.recent_quakes_count})`, list: v.recent_quakes.map(q => ({
        title: q.entity.title, subtitle: q.entity.subtitle, detail: fmt.km(q.distance_km), entity: q.entity })) });
    }
    const rank = a.rank || 0;
    return {
      title: v.name, subtitle: [v.type, v.country].filter(Boolean).join(', ') || 'Volcano',
      badges,
      block: [
        { label: 'Elevation', value: v.elevation_m != null ? fmt.int(v.elevation_m) : '–', unit: 'm' },
        { label: 'Last eruption', value: v.last_eruption_label || '–' },
        { label: 'Alert', value: a.hans?.color ? cap(a.hans.color) : rank >= 2 ? 'Active' : 'None' },
        { label: 'Quakes nearby, 7 days', value: fmt.int(v.recent_quakes_count || 0) },
      ],
      sections,
      links: [linkIf('Smithsonian GVP page and photos', v.page), linkIf('USGS notice', a.hans?.notice_url), linkIf('GDACS report', a.gdacs?.report_url),
        a.eonet ? linkIf('EONET event', (a.eonet.sources || [])[0]?.url) : null].filter(Boolean),
      actions: [kit.compareAction(v.lat, v.lon, 12, imageryDates(now(), 30, 0))],
      entity: ent(d, entity),
      foot: 'Global Volcanism Program, Smithsonian Institution (Volcanoes of the World)',
      favPayload: { level: VOLCANO_LEVEL[rank] },
    };
  }

  // -------------------------------------------------------------------- warning
  function warningCard(d, entity) {
    const w = d.data;
    const nowS = now();
    const upcoming = w.onset && w.onset > nowS + 600;
    const badges = [{ text: w.severity || 'Unknown severity', tone: TONE_SEV[w.severity] || 'grey' }];
    if (w.level && w.source === 'meteoalarm') badges.push({ text: `${w.level.charAt(0).toUpperCase()}${w.level.slice(1)} warning`, tone: { red: 'red', orange: 'amber', yellow: 'amber' }[w.level] || 'grey' });
    if (upcoming) badges.push({ text: 'Upcoming', tone: 'blue' });
    const urgency = { Immediate: 'Act now', Expected: 'Act soon' }[w.urgency];
    if (urgency) badges.push({ text: urgency, tone: 'grey', title: `CAP urgency: ${w.urgency}` });
    if (w.certainty && w.certainty !== 'Unknown') badges.push({ text: w.certainty, tone: 'grey', title: 'How sure the issuer is that it will happen (CAP certainty)' });
    const sevRank = { Minor: 1, Moderate: 2, Severe: 3, Extreme: 4 }[w.severity] || 0;
    const sections = [];
    if (w.headline || w.nws_headline) sections.push({ text: sentence(w.nws_headline || w.headline) });
    if (w.description) sections.push({ title: 'What to expect', el: expandable(w.description, 700) });
    if (w.instruction) sections.push({ title: 'What to do', el: expandable(w.instruction, 400) });
    sections.push({ rows: [
      ['Issued by', w.issuer || w.sender], ['Sent', w.sent ? kit.when(w.sent) : null], ['Starts', w.onset ? kit.when(w.onset) : null],
      ['Ends', w.expires ? kit.when(w.expires) : null], ['Hazard', w.hazard], ['Max wind gust', w.max_wind_gust], ['Max hail', w.max_hail_size ? `${w.max_hail_size} in` : null],
      ['Response', w.response && w.response !== 'None' ? w.response : null], ['Area', clip(w.area, 300)],
    ] });
    if (w.zone_names?.length > 1) sections.push({ title: `Zones (${w.zone_names.length})`, text: clip(w.zone_names.join(', '), 500) });
    const alsoBox = h('div', null, h('p', { class: 'dim' }, 'Checking other warnings here…'));
    const lat = entity.lat ?? w.lat;
    const lon = entity.lon ?? w.lon;
    if (ctx.dom.validLatLon(lat, lon)) sections.push({ title: 'Also in force here', el: alsoBox });
    const ends = w.expires ? fmt.clock(w.expires).slice(0, 5) : '–';
    return {
      title: warningName(w.event, w.hazard), subtitle: clip([w.area, w.country_name || (w.source === 'nws' ? 'United States' : '')].filter(Boolean).join(', '), 140),
      badges,
      block: [
        { label: 'Severity', value: w.severity || '–' },
        { label: upcoming ? 'Starts, UTC' : 'Since, UTC', value: w.onset ? fmt.clock(w.onset).slice(0, 5) : '–', unit: w.onset ? dayLabel(w.onset) : '' },
        { label: 'Ends, UTC', value: ends, unit: w.expires ? dayLabel(w.expires) : '' },
      ],
      sections,
      // the MeteoAlarm feed links to meteoalarm.org; a CAP-only link goes to the issuing service instead
      links: [linkIf(w.source === 'nws' ? 'Forecast and hazards at weather.gov'
        : /^https:\/\/([\w-]+\.)*meteoalarm\.org([/?#]|$)/i.test(w.web || '') ? 'MeteoAlarm' : clip(w.issuer, 60) || 'Issuing weather service', w.web),
        linkIf('CAP message', w.cap_url || (w.api_url && w.api_url.startsWith('https://') ? w.api_url : null))].filter(Boolean),
      entity: ent(d, { ...entity, lat, lon }),
      foot: w.source === 'nws' ? 'US National Weather Service' : `MeteoAlarm${w.issuer ? `, issued by ${w.issuer}` : ''}`,
      after: (card) => {
        if (w.outline) kit.select(card.key, w.outline, SEVERITY[sevRank] || ACCENT);
        if (ctx.dom.validLatLon(lat, lon)) fillAlso(alsoBox, lat, lon, w.id);
      },
    };
  }

  async function fillAlso(box, lat, lon, selfId) {
    try {
      const r = await ctx.api.get('/api/hazards/here', { lat: (+lat).toFixed(4), lon: (+lon).toFixed(4) });
      const others = (r.warnings || []).filter(x => x.id !== selfId);
      ctx.dom.clear(box);
      if (!others.length) { box.append(h('p', { class: 'dim' }, 'No other warnings at this spot.')); return; }
      const ul = h('ul', { class: 'card-list' });
      for (const o of others) {
        const b = h('button', { type: 'button' }, h('span', { class: 'li-d' }, o.severity || ''), h('span', { class: 'li-t' }, warningName(o.title)),
          h('span', { class: 'li-s' }, o.expires ? `Until ${fmt.utc(o.expires)}` : o.subtitle));
        b.addEventListener('click', () => ctx.entities.open({ ...o, title: warningName(o.title), lat, lon }, { fly: false }));
        ul.append(h('li', null, b));
      }
      box.append(ul);
    } catch {
      ctx.dom.clear(box);
      box.append(h('p', { class: 'dim' }, 'Could not check for other warnings.'));
    }
  }

  // -------------------------------------------------------------------- tsunami
  function tsunamiCard(d, entity) {
    const t = d.data;
    const tone = { Warning: 'red', Threat: 'red', Watch: 'amber', Advisory: 'amber', Information: 'blue' }[t.category] || 'grey';
    const sections = [];
    if (t.note) sections.push({ title: 'Note', text: t.note });
    if (t.definition) sections.push({ title: `What this ${t.category.toLowerCase()} level means`, text: t.definition });
    sections.push({ rows: [['Issued', kit.when(t.issued)], ['Earthquake region', sentence(t.region)], ['Position', pos(t.lat, t.lon)],
      ['Centre', t.center_name], ['Message', sentence(t.title)]] });
    if (t.quake) sections.push({ title: 'Earthquake', list: [{ title: t.quake.title, subtitle: t.quake.subtitle, entity: t.quake }] });
    return {
      title: `Tsunami ${t.category.toLowerCase()} message`, subtitle: `${sentence(t.region)}, ${t.center_name}`,
      badges: [{ text: t.category, tone }, { text: t.center === 'PHEB' ? 'Pacific centre' : 'National centre', tone: 'grey' }],
      block: [
        { label: 'Magnitude', value: t.magnitude != null ? fmt.num(t.magnitude, 1) : '–', unit: t.magnitude_type || '' },
        { label: 'Level', value: t.category },
        { label: 'Issued, UTC', value: t.issued ? fmt.clock(t.issued).slice(0, 5) : '–', unit: t.issued ? dayLabel(t.issued) : '' },
      ],
      sections,
      links: [linkIf('Bulletin', t.bulletin_url), linkIf('CAP message', t.cap_url), { text: 'tsunami.gov', href: 'https://www.tsunami.gov/' }].filter(Boolean),
      entity: ent(d, entity),
      foot: 'NOAA tsunami warning centres. Follow local authorities in an emergency.',
    };
  }

  // -------------------------------------------------------------------- outbreak
  function outbreakCard(d, entity) {
    const o = d.data;
    const badges = [o.active ? { text: 'Reported in the past 60 days', tone: 'amber' } : { text: 'Older report', tone: 'grey' }];
    if (o.high_consequence) badges.push({ text: 'High-consequence disease', tone: 'red' });
    if ((o.places || []).length > 1) badges.push({ text: `${o.places.length} countries`, tone: 'grey' });
    badges.push({ text: o.don, tone: 'grey' });
    const sections = [];
    if (o.summary) sections.push({ title: 'Summary', el: expandable(o.summary, 600) });
    for (const [title, body] of Object.entries(o.sections || {})) {
      if (['Overview', 'WHO risk assessment', 'WHO advice'].includes(title)) sections.push({ title, el: expandable(body, 360) });
    }
    sections.push({ rows: [['Published', kit.when(o.published)], ['Countries', (o.places || []).map(p => p.name).join(', ') || o.place_text],
      ['Region', o.region]] });
    if (o.related?.length) {
      sections.push({ title: 'Earlier reports', list: o.related.map(r => ({ title: r.title, subtitle: fmt.utc(r.published), entity: r.entity })) });
    }
    return {
      title: o.disease, subtitle: o.place_text || 'Location not stated',
      badges,
      block: [
        { label: 'Published', value: agoShort(o.published), unit: 'ago' },
        { label: 'Countries', value: fmt.int((o.places || []).length) },
        { label: 'Reports so far', value: fmt.int(1 + (o.related || []).length) },
      ],
      sections,
      links: [linkIf('WHO Disease Outbreak News', o.url)].filter(Boolean),
      entity: ent(d, entity),
      foot: 'World Health Organization, Disease Outbreak News',
    };
  }

  function expandable(body, n) {
    const full = String(body || '');
    if (full.length <= n + 40) return text(full);
    const el = text(clip(full, n));
    const more = h('button', { type: 'button', class: 'hazards-more' }, 'Show all');
    const wrap = h('div', null, el, more);
    more.addEventListener('click', () => { el.textContent = full; more.remove(); });
    return wrap;
  }

  const BUILDERS = { quake: quakeCard, fire: fireCard, natural: naturalCard, disaster: disasterCard, storm: stormCard,
    volcano: volcanoCard, warning: warningCard, tsunami: tsunamiCard, outbreak: outbreakCard };
  return { open };
}

function compass(deg) {
  if (deg == null || Number.isNaN(+deg)) return '–';
  const pts = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return pts[Math.round(((+deg % 360) + 360) % 360 / 22.5) % 16];
}

export function cap(s) {
  const t = String(s || '');
  return t ? `${t.charAt(0).toUpperCase()}${t.slice(1).toLowerCase()}` : '';
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function dayLabel(t) {
  const d = new Date(t * 1000);
  const today = new Date();
  const same = (a, b) => a.getUTCFullYear() === b.getUTCFullYear() && a.getUTCMonth() === b.getUTCMonth() && a.getUTCDate() === b.getUTCDate();
  if (same(d, today)) return 'today';
  if (same(d, new Date(today.getTime() + 86400000))) return 'tomorrow';
  if (same(d, new Date(today.getTime() - 86400000))) return 'yesterday';
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

export function agoShort(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h`;
  if (s < 86400 * 60) return `${Math.round(s / 86400)} days`;
  if (s < 86400 * 730) return `${Math.round(s / (86400 * 30.4))} months`;
  return `${Math.round(s / (86400 * 365.25))} years`;
}

function startCell(t) {
  if (!t) return { label: 'Started', value: '–' };
  const days = (Date.now() / 1000 - t) / 86400;
  if (days > 730) return { label: 'Active since', value: String(new Date(t * 1000).getUTCFullYear()) };
  return { label: 'Started', value: agoShort(t), unit: 'ago' };
}
