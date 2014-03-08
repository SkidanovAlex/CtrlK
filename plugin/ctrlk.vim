try
  if !l9#guardScriptLoading(expand('<sfile>:p'), 0, 0, [])
    finish
  endif
catch /E117/
  echoerr '***** Please install L9 plugin *****'
  finish
endtry

try
  python import clang
catch
  echoerr '***** Please install clang module for python *****'
  finish
endtry

try
  python import ctrlk
catch
  echoerr '***** Please install ctrlk module for python *****'
  finish
endtry

au FileType c,cpp,objc,objcpp call <SID>CtrlKInitBuffer()

call l9#defineVariableDefault('g:ctrlk_clang_library_path'        , '')

let s:plugin_path = escape(expand('<sfile>:p:h'), '\')
exe 'pyfile ' . fnameescape(s:plugin_path) . '/ctrlk_plugin.py'

function! CtrlKNavigate(entry, mode)
    python NavigateToEntry(vim.eval('a:entry'))
endfunction

function! CtrlKNavigateSymbols()
    try
      call fuf#suffixNumber('')
    catch /E117/
      echoerr '***** Please install FuzzyFinder plugin *****'
      finish
    endtry

    let s:my_items = []
    call fuf#fufctrlk#launch('', 1, 'navigate C++>', {'onComplete': function('CtrlKNavigate')}, s:my_items, 0)
endfunction

function! GetCtrlKState()
    python GetCtrlKState()
endfunction

function! ResetCtrlK()
    python ResetIndex()
endfunction

function! CtrlKGetCurrentScope()
    if !exists('b:current_scope')
        return ''
    endif
    return b:current_scope
endfunction

function! CtrlKGoToDefinition()
    python GoToDefinition('')
endfunction

function! CtrlKGoToDefinitionAndSplit(mode)
    python GoToDefinition(vim.eval('a:mode'))
endfunction

function! CtrlKGetReferences()
    python vim.command('let l:list = ' + json.dumps(FindReferences()))
    if !empty(l:list)
        copen
        call setqflist(l:list)
    else
        cclose
    endif
endfunction

function! s:ReadyToParse()
    if b:changedtick == b:my_changedtick
        return
    endif
    let b:my_changedtick = b:changedtick
    python RequestParse()
endfunction

function! s:OnBufferUnload(fname)
    python CtrlKBufferUnload(vim.eval('a:fname'))
endfunction

function! s:UpdateCurrentScope()
    python vim.command('let b:current_scope = "' + GetCurrentScopeStr() + '"')
endfunction

function! CtrlKStartFollowDefinition()
    rightbelow 20split
    if !exists('w:ctrlkfl') | let w:ctrlkfl=1 | endif
endfunction

function CtrlKOpenFileInFollowWindow(fname, line)
    let l:saved = winnr()
    for winnr in range(1, winnr('$'))
        if getwinvar(winnr, 'ctrlkfl') is 1
            execute winnr."wincmd w"
            if expand('%') != a:fname
                execute 'edit '.a:fname
            endif
            execute a:line
            norm! zt
            execute l:saved."wincmd w"
            return
        endif
    endfor
    call CtrlKStartFollowDefinition()
    call CtrlKOpenFileInFollowWindow(a:fname, a:line)
    execute l:saved."wincmd w"
endfunction

function! s:CtrlKInitBuffer()
    let b:my_changedtick = 0

    augroup CtrlK
        autocmd!
        autocmd VimLeave * python LeaveCtrlK()
        au CursorHold,CursorHoldI,InsertLeave,BufEnter,BufRead,FileType <buffer> call <SID>ReadyToParse()
        au BufUnload <buffer> call <SID>OnBufferUnload(expand('<afile>:p'))
"        au CursorMoved,CursorMovedI <buffer> call <SID>UpdateCurrentScope()
        autocmd CursorMoved,CursorMovedI <buffer> call CtrlKGoToDefinitionAndSplit('f')
    augroup END
endfunction

python InitCtrlK(vim.eval('g:ctrlk_clang_library_path'))

