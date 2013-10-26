if !l9#guardScriptLoading(expand('<sfile>:p'), 0, 0, [])
  finish
endif

au FileType c,cpp,objc,objcpp call <SID>CtrlKInitBuffer()

call l9#defineVariableDefault('g:ctrlk_clang_library_path'        , '')

let s:plugin_path = escape(expand('<sfile>:p:h'), '\')
exe 'pyfile ' . fnameescape(s:plugin_path) . '/ctrlk.py'

function! CtrlKNavigate(entry, mode)
    python NavigateToEntry(vim.eval('a:entry'))
endfunction

function! CtrlKNavigateSymbols()
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
    python GoToDefinition()
endfunction

function! CtrlKGetReferences()
    python vim.command('let l:list = ' + str(FindReferences()))
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

function! s:UpdateCurrentScope()
    python vim.command('let b:current_scope = "' + GetCurrentScopeStr() + '"')
endfunction

function! s:CtrlKInitBuffer()
    let b:my_changedtick = 0

    augroup CtrlK
        autocmd!
        autocmd VimLeave * python LeaveCtrlK()
        au CursorHold,CursorHoldI,InsertLeave,BufEnter,BufRead,FileType <buffer> call <SID>ReadyToParse()
"        au CursorMoved,CursorMovedI <buffer> call <SID>UpdateCurrentScope()
    augroup END
endfunction

python InitCtrlK(vim.eval('g:ctrlk_clang_library_path'))

