import React from 'react';
import {createRoot} from 'react-dom/client';
import AuthGate,{installCsrf} from '../app/sign-in';
installCsrf();
createRoot(document.getElementById('root')!).render(<AuthGate/>);
