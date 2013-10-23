if !l9#guardScriptLoading(expand('<sfile>:p'), 0, 0, [])
  finish
endif

call l9#defineVariableDefault('g:ctrlk_clang_library_path'        , '')

let s:plugin_path = escape(expand('<sfile>:p:h'), '\')
exe 'pyfile ' . fnameescape(s:plugin_path) . '/ctrlk.py'

function! CtrlKNavigate(entry, mode)
    python NavigateToEntry(vim.eval('a:entry'))
endfunction

function! RunCtrlK()
    python vim.command('let s:my_items = ' + str(GetItemsMatchingPattern('', int(vim.eval('g:fuf_enumeratingLimit')) + 1)))
    call fuf#fufctrlk#launch('', 1, 'navigate C++>', {'onComplete': function('CtrlKNavigate')}, s:my_items, 0)
endfunction

function! GetCtrlKState()
    python GetCtrlKState()
endfunction

augroup CtrlK
    autocmd VimLeave * python LeaveCtrlK()
augroup END

python InitCtrlK(vim.eval('g:ctrlk_clang_library_path'))

