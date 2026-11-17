'use strict';
// Shared calculations for personal reviews, team summaries and exports.
window.Scoring = {
  hasScore: n => typeof n === 'number' && Number.isFinite(n),
  mode: r => r?.mode === 'page' ? 'page' : 'deck',
  page: (r, image) => r?.pages?.[image.id] || {scores:{},note:''},
  stats(r, deck, dimensions) {
    const perPage = this.mode(r) === 'page';
    const entries = perPage ? (deck?.images || []).map(im => this.page(r,im)) : [r || {scores:{}}];
    const values = entries.flatMap(entry => dimensions.map(dim => entry.scores?.[dim.id])).filter(this.hasScore);
    const scores = Object.fromEntries(dimensions.map(dim => {
      const numbers = entries.map(entry => entry.scores?.[dim.id]).filter(this.hasScore);
      return [dim.id, numbers.length ? numbers.reduce((a,b)=>a+b,0)/numbers.length : null];
    }));
    const pagesDone = entries.filter(entry => dimensions.every(dim => this.hasScore(entry.scores?.[dim.id]))).length;
    return {scores, values, complete: entries.length > 0 && pagesDone === entries.length,
      mean: values.length ? values.reduce((a,b)=>a+b,0)/values.length : null,
      pagesDone: perPage ? pagesDone : 0, totalPages: perPage ? entries.length : 0};
  },
  rescale(r, oldMax, newMax) {
    for(const entry of [r,...Object.values(r.pages || {})])
      for(const key of Object.keys(entry.scores || {}))
        if(this.hasScore(entry.scores[key]))entry.scores[key]=Math.round(entry.scores[key]/oldMax*newMax*100)/100;
  }
};
