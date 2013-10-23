" This file is fuf/callbackitem (FuzzyFinder) with changes necessary for CtrlK
"
"=============================================================================
" Copyright for the original fuf/callbackitem.vim (c) 2007-2010 Takeshi NISHIDA
"
"=============================================================================
" LOAD GUARD {{{1

if !l9#guardScriptLoading(expand('<sfile>:p'), 0, 0, [])
  finish
endif

"let s:plugin_path = escape(expand('<sfile>:p:h'), '\')
"exe 'pyfile ' . fnameescape(s:plugin_path) . '/../ctrlk.py'

let s:mode_added = 0

" }}}1
"=============================================================================
" GLOBAL FUNCTIONS {{{1

"
function fuf#fufctrlk#createHandler(base)
  unlet a:base.getMatchingCompleteItems
  return a:base.concretize(copy(s:handler))
endfunction

"
function fuf#fufctrlk#getSwitchOrder()
  return -1
endfunction

"
function fuf#fufctrlk#getEditableDataNames()
  return []
endfunction

"
function fuf#fufctrlk#renewCache()
endfunction

"
function fuf#fufctrlk#requiresOnCommandPre()
  return 0
endfunction

"
function fuf#fufctrlk#onInit()
endfunction

"
function fuf#fufctrlk#launch(initialPattern, partialMatching, prompt, listener, items, forPath)
  if !s:mode_added
      call fuf#addMode('fufctrlk')
      let s:mode_added = 1
  endif

  let s:prompt = (empty(a:prompt) ? '>' : a:prompt)
  let s:listener = a:listener
  let s:forPath = a:forPath
  let s:items = copy(a:items)
  if s:forPath
    call map(s:items, 'fuf#makePathItem(v:val, "", 1)')
    call fuf#mapToSetSerialIndex(s:items, 1)
    call fuf#mapToSetAbbrWithSnippedWordAsPath(s:items)
  else
    call map(s:items, 'fuf#makeNonPathItem(v:val, "")')
    call fuf#mapToSetSerialIndex(s:items, 1)
    call map(s:items, 'fuf#setAbbrWithFormattedWord(v:val, 1)')
  endif
  call fuf#launch(s:MODE_NAME, a:initialPattern, a:partialMatching)
endfunction

" }}}1
"=============================================================================
" LOCAL FUNCTIONS/VARIABLES {{{1

let s:MODE_NAME = expand('<sfile>:t:r')

" }}}1
"=============================================================================
" s:handler {{{1

let s:handler = {}

"
function s:handler.getModeName()
  return s:MODE_NAME
endfunction

"
function s:handler.getPrompt()
  return fuf#formatPrompt(s:prompt, self.partialMatching, '')
endfunction

"
function s:handler.getPreviewHeight()
  if s:forPath
    return g:fuf_previewHeight
  endif
  return 0
endfunction

"
function s:handler.isOpenable(enteredPattern)
  return 1
endfunction

"
function s:handler.makePatternSet(patternBase)
  let parser = (s:forPath
        \       ? 's:interpretPrimaryPatternForPath'
        \       : 's:interpretPrimaryPatternForNonPath')
  return fuf#makePatternSet(a:patternBase, parser, self.partialMatching)
endfunction

"
function s:handler.makePreviewLines(word, count)
  if s:forPath
    return fuf#makePreviewLinesForFile(a:word, a:count, self.getPreviewHeight())
  endif
  return []
endfunction

"
function s:handler.getCompleteItems(patternPrimary)
  return s:items
endfunction

"
function s:handler.getMatchingCompleteItems2(patternBase)
  let patternSet = self.makePatternSet(a:patternBase)
  let exprBoundary = s:makeFuzzyMatchingExpr('a:wordForBoundary', patternSet.primaryForRank)
  let stats = filter(
        \ copy(self.stats), 'v:val.pattern ==# patternSet.primaryForRank')
  let items = self.getCompleteItems(patternSet.primary)
  " NOTE: In order to know an excess, plus 1 to limit number
  let items = l9#filterWithLimit(
        \ items, patternSet.filteringExpr, g:fuf_enumeratingLimit + 1)
  return map(items,
        \ 's:setRanks(v:val, patternSet.primaryForRank, exprBoundary, stats)')
endfunction

function! s:handler.getMatchingCompleteItems(patternBase)
  let patternSet = self.makePatternSet(a:patternBase)
  let exprBoundary = s:makeFuzzyMatchingExpr('a:wordForBoundary', patternSet.primaryForRank)
  let stats = filter(
        \ copy(self.stats), 'v:val.pattern ==# patternSet.primaryForRank')

  " NOTE: In order to know an excess, plus 1 to limit number
  python vim.command('let s:my_items = ' + str(GetItemsMatchingPattern(vim.eval('a:patternBase'), int(vim.eval('g:fuf_enumeratingLimit')) + 1)))

  call map(s:my_items, 'fuf#makeNonPathItem(v:val, "")')
  call fuf#mapToSetSerialIndex(s:my_items, 1)
  call map(s:my_items, 'fuf#setAbbrWithFormattedWord(v:val, 1)')

  return map(s:my_items,
        \ 's:setRanks(v:val, patternSet.primaryForRank, exprBoundary, stats)')
endfunction

"
function s:handler.onOpen(word, mode)
  call s:listener.onComplete(a:word, a:mode)
endfunction

"
function s:handler.onModeEnterPre()
endfunction

"
function s:handler.onModeEnterPost()
endfunction

"
function s:handler.onModeLeavePost(opened)
  if !a:opened && exists('s:listener.onAbort()')
    call s:listener.onAbort()
  endif
endfunction

" }}}1
"=============================================================================
" vim: set fdm=marker:
"
" a:pattern: 'str' -> '\V\.\*s\.\*t\.\*r\.\*'
function s:makeFuzzyMatchingExpr(target, pattern)
  let wi = ''
  for c in split(a:pattern, '\zs')
    if wi =~# '[^*?]$' && c !~ '[*?]'
      let wi .= '*'
    endif
    let wi .= c
  endfor
  return s:makePartialMatchingExpr(a:target, wi)
endfunction

" a:pattern: 'str' -> '\Vstr'
"            'st*r' -> '\Vst\.\*r'
function s:makePartialMatchingExpr(target, pattern)
  let patternMigemo = s:makeAdditionalMigemoPattern(a:pattern)
  if a:pattern !~ '[*?]' && empty(patternMigemo)
    " NOTE: stridx is faster than regexp matching
    return 'stridx(' . a:target . ', ' . string(a:pattern) . ') >= 0'
  endif
  return a:target . ' =~# ' .
        \ string(l9#convertWildcardToRegexp(a:pattern)) . patternMigemo
endfunction

" 
function s:makeAdditionalMigemoPattern(pattern)
  if !g:fuf_useMigemo || a:pattern =~# '[^\x01-\x7e]'
    return ''
  endif
  return '\|\m' . substitute(migemo(a:pattern), '\\_s\*', '.*', 'g')
endfunction

function s:setRanks(item, pattern, exprBoundary, stats)
  "let word2 = substitute(a:eval_word, '\a\zs\l\+\|\zs\A', '', 'g')
  let a:item.ranks = [
        \   s:evaluateLearningRank(a:item.word, a:stats),
        \   -s:scoreSequentialMatching(a:item.wordForRank, a:pattern),
        \   -s:scoreBoundaryMatching(a:item.wordForBoundary, 
        \                            a:pattern, a:exprBoundary),
        \   a:item.index,
        \ ]
  return a:item
endfunction

" 
function s:evaluateLearningRank(word, stats)
  for i in range(len(a:stats))
    if a:stats[i].word ==# a:word
      return i
    endif
  endfor
  return len(a:stats)
endfunction

" range of return value is [0.0, 1.0]
function s:scoreSequentialMatching(word, pattern)
  if empty(a:pattern)
    return str2float('0.0')
  endif
  let pos = stridx(a:word, a:pattern)
  if pos < 0
    return str2float('0.0')
  endif
  let lenRest = len(a:word) - len(a:pattern) - pos
  return str2float(pos == 0 ? '0.5' : '0.0') + str2float('0.5') / (lenRest + 1)
endfunction

" range of return value is [0.0, 1.0]
function s:scoreBoundaryMatching(wordForBoundary, pattern, exprBoundary)
  if empty(a:pattern)
    return str2float('0.0')
  endif
  if !eval(a:exprBoundary)
    return 0
  endif
  return (s:scoreSequentialMatching(a:wordForBoundary, a:pattern) + 1) / 2
endfunction

